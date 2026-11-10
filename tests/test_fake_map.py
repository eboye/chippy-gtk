import base64
import json
import zlib

import pytest

from chippy.core import fake
from chippy.core.device import DeviceError
from chippy.core.fake import (
    FAKE_LAYOUT,
    FakeMap,
    FakeVacuum,
    build_fake_payload,
    fake_live_payload,
)
from chippy.core.mapdecode import decode_map
from chippy.core.mapedit import MapZones, Rect
from chippy.mapmodel import grid_from_frame, room_areas


def test_fake_live_robot_moves_inside_room_1():
    from chippy.mapmodel import grid_from_frame, live_from_frame

    frames = [decode_map(fake_live_payload(step)) for step in (0, 3, 6)]
    positions = [f.robot[:2] for f in frames]
    assert len(set(positions)) == 3
    assert len(frames[2].path) > len(frames[0].path)
    grid = grid_from_frame(frames[0])
    for f in frames:
        live = live_from_frame(f, frames[0], grid)
        assert grid.seg_at(int(live.robot[0]), int(live.robot[1])) == 1
        assert all(grid.seg_at(int(c), int(r)) == 1 for c, r in live.path)


def test_fake_live_loop_wraps_and_faces_its_direction():
    first, wrapped = decode_map(fake_live_payload(0)), decode_map(fake_live_payload(24))
    assert first.robot == wrapped.robot and first.path == wrapped.path
    assert decode_map(fake_live_payload(1)).robot[2] == 0  # walking right along the top edge


def test_fake_vacuum_live_object_only_while_active():
    vac = FakeVacuum()
    assert vac.request_live_map_object() is None
    vac.start()
    vac.poll()
    assert vac.request_live_map_object() == "fake-live"
    step = vac.live_step
    vac.poll()
    assert vac.live_step == step + 1
    vac.dock()
    assert vac.request_live_map_object() == "fake-live"
    vac.stop()
    assert vac.request_live_map_object() is None


def test_fake_layout_rows_are_equal_length():
    assert len({len(row) for row in FAKE_LAYOUT}) == 1


def test_fake_payload_round_trips():
    frame = decode_map(build_fake_payload())
    assert (frame.width, frame.height) == (len(FAKE_LAYOUT[0]), len(FAKE_LAYOUT))
    assert frame.names == {
        1: "Living room",
        2: "Kitchen",
        3: "Bedroom",
        4: "Hallway",
        5: "Room 5",
    }
    # Stored bottom-up: stored row r is layout row height - 1 - r.
    for r in range(frame.height):
        stored = frame.pixels[r * frame.width : (r + 1) * frame.width]
        expected = FAKE_LAYOUT[frame.height - 1 - r]
        assert bytes(p & 0x3F for p in stored) == bytes(
            int(c) if c.isdigit() else 0 for c in expected
        )
    assert frame.charger is not None


def test_fake_payload_marks_room_edges():
    frame = decode_map(build_fake_payload())

    def cell(layout_row, col):
        r = frame.height - 1 - layout_row
        return frame.pixels[r * frame.width + col]

    # Layout row 1 is ".22222222..111...": col 8 is the last '2' (right neighbour '.'),
    # col 3 is interior (right neighbour '2'), col 0 is outside.
    assert cell(1, 8) == 0x80 | 2
    assert cell(1, 3) == 2
    assert cell(1, 0) == 0


def _trailer(payload: bytes) -> dict:
    doc = json.loads(payload)
    raw = zlib.decompress(base64.urlsafe_b64decode(doc["mapstr"][0]["map"]))
    return json.loads(raw[raw.index(b'{"') :])


def test_fake_robot_applies_zones_and_names():
    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    vac.update_map(MapZones(no_go=(Rect(0, 0, 600, 600),)).payload())
    vac.update_map({"nsr": {"2": {"name": "S2l0Y2hlbiAy", "type": 0, "index": 0}}, "mapid": 0})
    frame = decode_map(build_fake_payload(fake_map=fm))
    assert frame.zones.no_go == (Rect(0, 0, 600, 600),)
    assert frame.names[2] == "Kitchen 2"
    assert len(vac.edits) == 2


def test_fake_robot_merges_and_splits():
    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    vac.update_map({"msr": [1, 2], "mapid": 0})
    areas = room_areas(grid_from_frame(decode_map(build_fake_payload(fake_map=fm))))
    assert 2 not in areas and areas[1] > 0
    assert "2" not in fm.cleanset and 2 not in fm.names
    before = set(areas)
    x, y = fake._cell_mm(20, 15)  # inside room 3 (bedroom)
    vac.update_map({"dsrid": [x, y - 2000, x, y + 2000, 3], "mapid": 0})
    after = set(room_areas(grid_from_frame(decode_map(build_fake_payload(fake_map=fm)))))
    assert len(after - before) == 1
    assert "6" in fm.cleanset


def test_fake_split_outside_room_and_unknown_keys_raise():
    vac = FakeVacuum(fake_map=FakeMap())
    x, y = fake._cell_mm(20, 15)
    with pytest.raises(DeviceError):
        vac.update_map({"dsrid": [x - 9000, y, x - 9000, y + 100, 3], "mapid": 0})
    with pytest.raises(DeviceError):
        vac.update_map({"msr": [1, 99], "mapid": 0})
    with pytest.raises(DeviceError):
        vac.update_map({"bogus": 1})


def test_fake_default_map_matches_today():
    fm = FakeMap()
    assert fm.cleanset == {str(i): [1, 2, 1, i, -1] for i in range(1, 6)}
    assert fm.room_settings is False and fm.names == fake.FAKE_NAMES
    assert decode_map(build_fake_payload()).zones == MapZones()
    assert build_fake_payload() == build_fake_payload(fake_map=FakeMap())


def test_fake_settings_and_order_and_switch():
    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    vac.update_map({"customeClean": [[3, 2, 3, 1]], "mapid": 0})
    assert fm.cleanset["3"][:3] == [2, 3, 1]
    assert fm.cleanset["3"][4] == -1
    vac.update_map({"cleanOrder": [3, 1, 2, 4, 5]})
    assert fm.cleanset["3"][3] == 1 and fm.cleanset["1"][3] == 2
    assert vac.poll().room_settings is False
    vac.set_room_settings_enabled(True)
    assert vac.poll().room_settings is True


def test_fake_live_frames_carry_cleanset():
    assert _trailer(fake_live_payload(2, FakeMap()))["cleanset"]["1"] == [1, 2, 1, 1, -1]
    assert "cleanset" not in _trailer(build_fake_payload())
