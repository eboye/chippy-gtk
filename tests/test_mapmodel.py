import pytest

from chippy.core.fake import FAKE_LAYOUT, build_fake_payload
from chippy.core.mapdecode import decode_map
from chippy.mapmodel import (
    PALETTE,
    Viewport,
    build_rooms,
    grid_from_frame,
    label_anchors,
    live_from_frame,
    room_areas,
    texture_rgba,
    tiny_rooms,
)


@pytest.fixture
def grid():
    return grid_from_frame(decode_map(build_fake_payload()))


def test_grid_is_in_display_orientation(grid):
    for row, line in enumerate(FAKE_LAYOUT):
        for col, ch in enumerate(line):
            assert grid.seg_at(col, row) == (int(ch) if ch.isdigit() else 0)
    assert grid.seg_at(-1, 0) == 0 and grid.seg_at(0, 999) == 0


def test_charger_in_display_cells(grid):
    col, row = grid.charger
    assert 36.5 <= col <= 37.5 and 1 <= row <= 3


def test_areas_and_tiny_rooms(grid):
    counts = room_areas(grid)
    assert counts[5] == 2
    assert grid.cell_area() == pytest.approx(0.04)
    assert tiny_rooms(grid) == {5}


def test_anchor_is_inside_u_shaped_room(grid):
    anchors = label_anchors(grid)
    for seg, (cx, cy) in anchors.items():
        assert grid.seg_at(int(cx), int(cy)) == seg


def test_anchor_prefers_the_deepest_cell_nearest_the_centroid(grid):
    cols = [
        col for row in range(grid.height) for col in range(grid.width) if grid.seg_at(col, row) == 1
    ]
    centroid_col = sum(cols) / len(cols) + 0.5
    anchor_col, _ = label_anchors(grid)[1]
    assert abs(anchor_col - centroid_col) <= 3


def test_live_from_frame_matches_charger_transform():
    saved = decode_map(build_fake_payload())
    grid = grid_from_frame(saved)
    live = live_from_frame(
        decode_map(
            build_fake_payload(
                robot=(saved.charger[0], saved.charger[1], 90),
                path=[saved.charger, (saved.charger[0] + 200, saved.charger[1])],
            )
        ),
        saved,
        grid,
    )
    assert live.robot == pytest.approx(grid.charger)
    assert live.angle == pytest.approx(90)
    assert live.path[1][0] == pytest.approx(live.path[0][0] + 1)  # +200 mm = +1 cell right


def test_live_from_frame_without_robot():
    saved = decode_map(build_fake_payload())
    live = live_from_frame(saved, saved, grid_from_frame(saved))
    assert live.robot is None and live.path == []


def test_build_rooms_names_colours_hidden(grid):
    rooms = build_rooms(grid, {1: "Living room", 4: "Hallway"}, {4: "Corridor"}, {5})
    assert [r.id for r in rooms] == [1, 2, 3, 4, 5]
    by_id = {r.id: r for r in rooms}
    assert by_id[1].name == "Living room"
    assert by_id[4].name == "Corridor"
    assert by_id[2].name == "Room 2"
    assert by_id[5].hidden and not by_id[1].hidden
    assert by_id[1].color == PALETTE[0] and by_id[2].color == PALETTE[1]
    assert by_id[2].area_m2 == pytest.approx(room_areas(grid)[2] * 0.04, abs=0.05)


def test_build_rooms_without_map_uses_names_only():
    rooms = build_rooms(None, {1: "A"}, {2: "B"}, set())
    assert [(r.id, r.name, r.area_m2, r.anchor) for r in rooms] == [
        (1, "A", None, None),
        (2, "B", None, None),
    ]


def test_viewport_fit_zoom_pan_round_trip():
    vp = Viewport(100, 50)
    vp.resize(400, 400)
    assert vp.fitted and vp.scale == pytest.approx(400 / 100 * 0.92)
    cx, cy = vp.to_cell(200, 200)
    vp.zoom_at(2.0, 200, 200)
    assert not vp.fitted
    assert vp.to_cell(200, 200) == pytest.approx((cx, cy))  # zoom keeps the point under the cursor
    vp.pan(10, -5)
    x, y = vp.to_widget(*vp.to_cell(123, 77))
    assert (x, y) == pytest.approx((123, 77))
    vp.zoom_at(1000, 0, 0)
    assert vp.scale == pytest.approx(vp.fit_scale() * 8)
    vp.fit()
    assert vp.fitted


def test_texture_dims_selection_and_outside(grid):
    rooms = build_rooms(grid, {}, {}, {5})
    data = texture_rgba(grid, rooms, {1}, background=(0, 0, 0))
    assert len(data) == grid.width * grid.height * 4

    def px(col, row):
        i = (row * grid.width + col) * 4
        return tuple(data[i : i + 4])

    assert px(0, 0)[3] == 0  # outside: transparent
    sel, unsel = px(20, 4), px(3, 3)  # room 1 interior (selected), room 2 interior
    assert sel[3] == 255 and unsel[3] == 255
    assert sum(sel[:3]) > sum(unsel[:3])  # unselected rooms are dimmed toward black
