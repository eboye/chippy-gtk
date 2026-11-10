import base64
import json

from chippy.core.mapedit import (
    DEFAULT_SETTING,
    EditSession,
    Line,
    MapEdit,
    MapZones,
    Rect,
    RoomSetting,
    merge_payload,
    parse_cleanset,
    remap_after_merge,
    remap_after_split,
    split_payload,
)
from chippy.core.profiles import R2209


def test_rect_normalises():
    assert Rect.of(500, 900, -100, 200) == Rect(-100, 200, 500, 900)


def test_zones_parse_r2209_empty_and_filled():
    assert MapZones.parse({"line": [], "rect": [], "mop": []}) == MapZones()
    vw = {"line": [[0, 0, 1000, 0]], "rect": [[300, 400, 100, 0]], "mop": [[1, 2, 3, 4]]}
    zones = MapZones.parse(vw)
    assert zones.walls == (Line(0, 0, 1000, 0),)
    assert zones.no_go == (Rect(100, 0, 300, 400),)
    assert zones.no_mop == (Rect(1, 2, 3, 4),)


def test_zones_parse_skips_junk():
    assert MapZones.parse(None) == MapZones()
    assert MapZones.parse({"rect": [[1, 2, 3]], "line": ["x"], "mop": 5}) == MapZones()


def test_zones_payload_is_the_complete_set():
    zones = MapZones(no_go=(Rect(0, 0, 500, 500),), walls=(Line(1, 2, 3, 4),))
    assert zones.payload() == {
        "vw": {"line": [[1, 2, 3, 4]], "rect": [[0, 0, 500, 500]], "mop": []}
    }


def test_parse_cleanset_from_a_live_frame():
    settings, order = parse_cleanset(
        {"1": [1, 3, 1, 2, -1], "2": [3, 1, 2, 1, -1], "x": [0]}, R2209
    )
    assert settings == {
        1: RoomSetting("standard", "high", 1),
        2: RoomSetting("turbo", "low", 2),
    }
    assert order == [2, 1]
    assert parse_cleanset("not a dict", R2209) is None
    assert parse_cleanset(json.dumps({"1": [0, 2, 1, 1]}), R2209)[0] == {
        1: RoomSetting("quiet", "medium", 1)
    }


def _session(**kw):
    base = dict(
        map_id=7,
        zones=MapZones(),
        names={1: "Kitchen", 2: "Bath"},
        settings={1: DEFAULT_SETTING, 2: DEFAULT_SETTING},
        order=[1, 2],
        use_room_settings=False,
        settings_known=True,
    )
    base.update(kw)
    return EditSession(**base)


def test_no_changes_no_edits():
    s = _session()
    assert s.changes(s.copy(), R2209) == []


def test_changes_in_fixed_order_with_exact_payloads():
    before = _session()
    after = before.copy()
    after.zones = MapZones(no_mop=(Rect(0, 0, 400, 400),))
    after.names[2] = "Bathroom"
    after.settings[1] = RoomSetting("turbo", "low", 2)
    after.order = [2, 1]
    after.use_room_settings = True
    edits = after.changes(before, R2209)
    assert [e.kind for e in edits] == ["zones", "names", "settings", "order", "room_settings"]
    assert edits[0].payload == {"vw": {"line": [], "rect": [], "mop": [[0, 0, 400, 400]]}}
    name = base64.b64encode(b"Bathroom").decode()
    assert edits[1].payload == {
        "nsr": {"2": {"name": name, "type": 0, "index": 0}},
        "mapid": 7,
    }
    assert edits[2].payload == {"customeClean": [[1, 3, 1, 2], [2, 1, 2, 1]]}
    assert edits[3].payload == {"cleanOrder": [2, 1]}
    assert edits[4] == MapEdit("room_settings", None, True)


def test_unknown_settings_are_sent_once_saved_even_if_unchanged():
    before = _session(settings_known=False)
    after = before.copy()
    after.use_room_settings = True
    kinds = [e.kind for e in after.changes(before, R2209)]
    assert kinds == ["settings", "room_settings"]


def test_merge_split_payloads():
    assert merge_payload(7, 3, 4) == {"msr": [3, 4], "mapid": 7}
    assert split_payload(7, 5, Line(0, 10, 900, 10)) == {"dsrid": [0, 10, 900, 10, 5], "mapid": 7}


def test_remap_names():
    names = {1: "Kitchen", 2: "Bath", 3: "Hall"}
    assert remap_after_merge(names, kept=2, removed=3) == {1: "Kitchen", 2: "Bath"}
    assert remap_after_split(names, seg=2, new_ids={11}) == {
        1: "Kitchen",
        2: "Bath",
        3: "Hall",
        11: "Bath 2",
    }
