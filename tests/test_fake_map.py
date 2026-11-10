from chippy.core.fake import FAKE_LAYOUT, FakeVacuum, build_fake_payload, fake_live_payload
from chippy.core.mapdecode import decode_map


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
