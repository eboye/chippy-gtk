import pytest

from chippy.core.fake import FAKE_LAYOUT, build_fake_payload
from chippy.core.mapdecode import decode_map
from chippy.core.mapedit import Line, MapZones, Rect
from chippy.mapmodel import (
    PALETTE,
    MapGeometry,
    Viewport,
    build_rooms,
    geometry_from_frame,
    grid_from_frame,
    hit_zone,
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


def test_geometry_matches_charger_transform_and_round_trips():
    frame = decode_map(build_fake_payload())
    geom = geometry_from_frame(frame)
    assert geom == MapGeometry(frame.left, frame.top, frame.grid_mm, frame.height)
    assert geom.to_cell(*frame.charger[:2]) == pytest.approx(grid_from_frame(frame).charger)
    assert geom.to_cell(frame.left, frame.top) == pytest.approx((0, frame.height))
    for x, y in ((0, 0), (1234, -567), (frame.left, frame.top), (-3999, 2001)):
        assert geom.to_mm(*geom.to_cell(x, y)) == (x, y)
    x, y = geom.to_mm(1.26, 3.74)
    assert isinstance(x, int) and isinstance(y, int)


# A 200 mm grid with the map origin at cell (0, 10): x mm = 200 cx, y mm = 200 (10 - cy).
GEOM = MapGeometry(left=0, top=0, grid_mm=200, height=10)
ZONES = MapZones(
    no_go=(Rect(0, 0, 1000, 1000), Rect(600, 600, 1600, 1600)),
    no_mop=(Rect(1000, 0, 2000, 400),),
    walls=(Line(0, 1400, 1000, 1400),),
)


def test_hit_zone_bodies_corners_and_misses():
    # no_go[0] spans cells x 0..5, y 5..10; its corner c0 (x1, y1) is cell (0, 10).
    assert hit_zone(ZONES, GEOM, 0.05, 9.95, 0.3) == ("no_go", 0, "c0")
    assert hit_zone(ZONES, GEOM, 5, 10, 0.3) == ("no_go", 0, "c1")  # (x2, y1)
    assert hit_zone(ZONES, GEOM, 0, 5, 0.3) == ("no_go", 0, "c3")  # (x1, y2)
    assert hit_zone(ZONES, GEOM, 1, 9, 0.3) == ("no_go", 0, "body")
    assert hit_zone(ZONES, GEOM, 7, 9.5, 0.3) == ("no_mop", 0, "body")
    assert hit_zone(ZONES, GEOM, 9, 1, 0.3) is None
    assert hit_zone(MapZones(), GEOM, 1, 9, 0.3) is None


def test_hit_zone_walls_ends_before_body():
    # Wall from cell (0, 3) to (5, 3).
    assert hit_zone(ZONES, GEOM, 0.1, 3.1, 0.3) == ("wall", 0, "end0")
    assert hit_zone(ZONES, GEOM, 5, 3, 0.3) == ("wall", 0, "end1")
    assert hit_zone(ZONES, GEOM, 2.5, 3.2, 0.3) == ("wall", 0, "body")
    assert hit_zone(ZONES, GEOM, 2.5, 3.5, 0.3) is None


def test_hit_zone_overlaps_topmost_wins_and_handles_beat_bodies():
    # no_go[1] (cells x 3..8, y 2..7) overlaps no_go[0] at cells x 3..5, y 5..7.
    assert hit_zone(ZONES, GEOM, 4, 6, 0.3) == ("no_go", 1, "body")
    # no_go[0]'s c2 (x2, y2) at cell (5, 5) lies inside no_go[1]'s body: the handle wins.
    assert hit_zone(ZONES, GEOM, 5, 5, 0.3) == ("no_go", 0, "c2")


def test_hit_zone_handle_reach_is_capped_for_small_zones():
    small = MapZones(no_go=(Rect(0, 1600, 200, 2000),))  # cells x 0..1, y 0..2
    assert hit_zone(small, GEOM, 0.5, 1, 0.8) == ("no_go", 0, "body")
    assert hit_zone(small, GEOM, 0.1, 0.1, 0.8) == ("no_go", 0, "c3")
    wall = MapZones(walls=(Line(0, 1000, 300, 1000),))  # 1.5 cells long
    assert hit_zone(wall, GEOM, 0.75, 5, 0.8) == ("wall", 0, "body")


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
