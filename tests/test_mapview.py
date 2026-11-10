import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from chippy.core.fake import build_fake_payload  # noqa: E402
from chippy.core.mapdecode import decode_map  # noqa: E402
from chippy.core.mapedit import Line, MapZones, Rect  # noqa: E402
from chippy.mapmodel import build_rooms, geometry_from_frame, grid_from_frame  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from chippy.ui.mapview import MapView  # noqa: E402


def test_room_at_and_hidden_rooms():
    grid = grid_from_frame(decode_map(build_fake_payload()))
    view = MapView()
    view.set_map(grid, build_rooms(grid, {}, {}, {5}))
    view.viewport.resize(840, 420)
    x, y = view.viewport.to_widget(20.5, 4.5)  # room 1 interior
    assert view.room_at(x, y) == 1
    x, y = view.viewport.to_widget(26.5, 19.5)  # room 5 (hidden)
    assert view.room_at(x, y) == 0
    assert set(view.label_points()) == {1, 2, 3, 4}


def _view():
    grid = grid_from_frame(decode_map(build_fake_payload()))
    view = MapView()
    view.set_map(grid, build_rooms(grid, {}, {}, {5}))
    view.viewport.resize(840, 420)
    return view


def test_drag_past_pan_threshold_suppresses_click():
    view = _view()
    clicks = []
    view.connect("room-clicked", lambda _v, seg: clicks.append(seg))
    x, y = view.viewport.to_widget(20.5, 4.5)
    view._on_drag_begin(None, x, y)
    view._on_drag_update(None, 5, 0)
    view._on_click(None, 1, x, y)
    assert clicks == []
    view._on_drag_begin(None, x, y)
    view._on_drag_update(None, 1, 0)
    view._on_click(None, 1, x, y)
    assert clicks == [1]


def test_double_click_fits_without_changing_the_selection():
    # GestureClick reports a double click as release n_press=1 then n_press=2; the first
    # toggles the room under the pointer, so the second must toggle it back.
    view = _view()
    clicks = []
    view.connect("room-clicked", lambda _v, seg: clicks.append(seg))
    view.viewport.zoom_at(2.0, 420, 210)
    x, y = view.viewport.to_widget(20.5, 4.5)
    view._on_drag_begin(None, x, y)
    view._on_click(None, 1, x, y)
    view._on_drag_begin(None, x, y)
    view._on_click(None, 2, x, y)
    assert clicks == [1, 1]  # toggled and toggled back: net unchanged
    assert view.viewport.scale == pytest.approx(view.viewport.fit_scale())
    # A double click on empty floor fits without any room event.
    clicks.clear()
    view._on_click(None, 1, 0, 0)
    view._on_click(None, 2, 0, 0)
    assert clicks == []
    # A later single click is an ordinary toggle again.
    x, y = view.viewport.to_widget(20.5, 4.5)
    view._on_click(None, 1, x, y)
    assert clicks == [1]


def test_scroll_scales_with_delta():
    view = _view()
    view._on_motion(None, 420, 210)
    before = view.viewport.scale
    view._on_scroll(None, 0, -0.1)
    assert 1.0 < view.viewport.scale / before < 1.05
    view.viewport.zoom_at(1.0, 0, 0)
    before = view.viewport.scale
    view._on_scroll(None, 0, -100)
    assert view.viewport.scale / before == pytest.approx(2.0)
    before = view.viewport.scale
    view._on_scroll(None, 0, 100)
    assert view.viewport.scale / before == pytest.approx(0.5)


def test_scroll_without_motion_zooms_around_centre():
    view = _view()
    view.get_width = lambda: 840
    view.get_height = lambda: 420
    cx, cy = view.viewport.to_cell(420, 210)
    view._on_scroll(None, 0, -1)
    assert view.viewport.to_widget(cx, cy) == pytest.approx((420, 210))


def test_set_live_redraws_without_rebuilding_the_texture():
    from chippy.mapmodel import LiveState

    view = _view()
    view._ensure_texture()
    texture = view._texture
    live = LiveState(robot=(20.5, 4.5), angle=90.0, path=[(14.5, 3.5), (20.5, 4.5)])
    view.set_live(live)
    assert view.live is live and view._texture is texture
    view.set_live(None)
    assert view.live is None and view._texture is texture


def test_texture_cache_and_clearing():
    view = _view()
    view._ensure_texture()
    assert view._texture is not None
    view.viewport.pan(10, 10)
    view.queue_draw()
    assert view._texture is not None
    view.set_selection({1})
    assert view._texture is None
    view._ensure_texture()
    view.set_rooms(view.rooms)
    assert view._texture is None
    view.set_map(None, [])
    assert view.viewport is None
    assert view.room_at(10, 10) == 0


def test_live_subtitle_names_the_state():
    from chippy.ui.window import live_subtitle

    assert live_subtitle("cleaning") == "Cleaning · robot position live"
    assert live_subtitle("returning") == "Returning · robot position live"


def test_heading_sticks_out_of_the_robot_dot():
    from chippy.ui.mapview import HEADING_LENGTH

    assert HEADING_LENGTH >= 9 + 2.5 + 6  # dot + ring, plus a clearly visible pointer


# Edit tools. The fake map: left -4000, top -2000, 200 mm cells, 21 rows; room 1 spans
# x -1800..3600 mm, y 400..2000 mm (cells x 11..38, rows 1..8).
GEOM = geometry_from_frame(decode_map(build_fake_payload()))
EMPTY = MapZones()
SIGNALS = ("zones-edited", "zone-selected", "room-picked", "split-line", "room-clicked")


def _edit_view(tool, zones=EMPTY, geom=GEOM):
    view = _view()
    view.set_zones(zones, geom)
    view.set_tool(tool)
    events = []
    for name in SIGNALS:
        view.connect(name, lambda _v, *args, name=name: events.append((name, *args)))
    return view, events


def _at(view, x_mm, y_mm):
    return view.viewport.to_widget(*GEOM.to_cell(x_mm, y_mm))


def _drag(view, start_mm, end_mm):
    (x0, y0), (x1, y1) = _at(view, *start_mm), _at(view, *end_mm)
    view._on_drag_begin(None, x0, y0)
    view._on_drag_update(None, (x1 - x0) / 2, (y1 - y0) / 2)
    view._on_drag_update(None, x1 - x0, y1 - y0)
    view._on_drag_end(None, x1 - x0, y1 - y0)
    view._on_click(None, 1, x1, y1)  # a drag's release must not count as a click


def _click(view, x_mm, y_mm):
    x, y = _at(view, x_mm, y_mm)
    view._on_drag_begin(None, x, y)
    view._on_drag_end(None, 0, 0)
    view._on_click(None, 1, x, y)


def test_no_go_drag_appends_a_normalised_rect_and_selects_it():
    view, events = _edit_view("no_go")
    _drag(view, (1000, 1600), (0, 1000))
    zones = MapZones(no_go=(Rect(0, 1000, 1000, 1600),))
    assert events == [("zones-edited", zones), ("zone-selected", "no_go", 0)]
    assert view.zones == zones and view.selected_zone == ("no_go", 0)
    events.clear()
    _drag(view, (0, 0), (200, 1000))  # 200 mm wide: too small
    assert events == [] and view.zones == zones


def test_no_mop_and_wall_drags_append():
    view, events = _edit_view("no_mop")
    _drag(view, (0, 1000), (400, 1400))
    assert view.zones.no_mop == (Rect(0, 1000, 400, 1400),)
    view.set_tool("wall")
    events.clear()
    _drag(view, (0, 1000), (200, 1100))  # ~224 mm: too short
    assert events == []
    _drag(view, (400, 600), (0, 600))
    assert view.zones.walls == (Line(400, 600, 0, 600),)
    assert events == [("zones-edited", view.zones), ("zone-selected", "wall", 0)]


def test_select_click_move_resize_and_delete():
    zones = MapZones(no_go=(Rect(0, 1000, 1000, 1600),))
    view, events = _edit_view("select", zones)
    _click(view, 500, 1300)
    assert events == [("zone-selected", "no_go", 0)]  # the zone, not the room under it
    events.clear()
    _drag(view, (500, 1300), (900, 1100))  # body: move by (+400, -200)
    moved = MapZones(no_go=(Rect(400, 800, 1400, 1400),))
    assert events == [("zones-edited", moved)] and view.zones == moved
    events.clear()
    _drag(view, (1400, 1400), (2000, 1800))  # c2 (x2, y2): resize
    resized = MapZones(no_go=(Rect(400, 800, 2000, 1800),))
    assert events == [("zones-edited", resized)]
    events.clear()
    _drag(view, (2000, 1800), (600, 1000))  # would leave 200 mm sides: rejected
    assert events == [] and view.zones == resized
    view._on_key(None, Gdk.KEY_Delete, 0, 0)
    assert events == [("zones-edited", MapZones()), ("zone-selected", "", -1)]
    assert view.selected_zone is None
    events.clear()
    assert view._on_key(None, Gdk.KEY_BackSpace, 0, 0) is False  # nothing selected
    assert events == []


def test_select_drag_selects_and_moves_a_wall_end():
    zones = MapZones(walls=(Line(0, 1000, 1000, 1000),))
    view, events = _edit_view("select", zones)
    _drag(view, (1000, 1000), (1000, 1600))
    edited = MapZones(walls=(Line(0, 1000, 1000, 1600),))
    assert events == [("zone-selected", "wall", 0), ("zones-edited", edited)]
    events.clear()
    view._on_key(None, Gdk.KEY_BackSpace, 0, 0)
    assert events == [("zones-edited", MapZones()), ("zone-selected", "", -1)]


def test_select_empty_click_deselects_room_click_picks_and_empty_drag_pans():
    zones = MapZones(no_go=(Rect(0, 1000, 1000, 1600),))
    view, events = _edit_view("select", zones)
    _click(view, 500, 1300)
    _click(view, 3000, 1200)  # room 1, away from the zone
    assert events == [
        ("zone-selected", "no_go", 0),
        ("zone-selected", "", -1),
        ("room-picked", 1),
    ]
    events.clear()
    view._on_click(None, 1, 0, 0)  # outside every room: nothing selected, nothing to say
    assert events == []
    ox = view.viewport.ox
    _drag(view, (3000, 1200), (3600, 1200))
    assert view.viewport.ox != ox and events == [] and view.zones == zones


def test_order_and_merge_pick_rooms_and_never_toggle_selection():
    for tool in ("order", "merge"):
        view, events = _edit_view(tool)
        _click(view, 3000, 1200)
        assert events == [("room-picked", 1)]


def test_split_picks_a_room_then_a_line():
    view, events = _edit_view("split")
    ox = view.viewport.ox
    _drag(view, (0, 1000), (1000, 1000))  # no room yet: pans
    assert events == [] and view.viewport.ox != ox
    view.viewport.fit()
    _click(view, 3000, 1200)
    assert events == [("room-picked", 1)]
    events.clear()
    _drag(view, (0, 1000), (200, 1000))  # too short
    assert events == []
    _drag(view, (0, 400), (0, 2000))
    assert events == [("split-line", 1, Line(0, 400, 0, 2000))]


def test_browsing_ignores_zones():
    zones = MapZones(no_go=(Rect(0, 1000, 1000, 1600),))
    view, events = _edit_view(None, zones)
    ox = view.viewport.ox
    _drag(view, (500, 1300), (900, 1300))
    assert view.viewport.ox != ox and events == [] and view.zones == zones
    view.viewport.fit()
    _click(view, 500, 1300)
    assert events == [("room-clicked", 1)]
    assert view._on_key(None, Gdk.KEY_Delete, 0, 0) is False


def test_without_geometry_tools_needing_mm_do_nothing():
    view, events = _edit_view("no_go", MapZones(), None)
    ox = view.viewport.ox
    _drag(view, (0, 1000), (1000, 1600))
    assert events == [] and view.viewport.ox == ox
    view.set_tool("select")
    _click(view, 3000, 1200)
    assert events == [("room-picked", 1)]


def test_set_zones_keeps_the_selection_only_for_the_same_zones():
    zones = MapZones(no_go=(Rect(0, 1000, 1000, 1600),))
    view, _events = _edit_view("select", zones)
    _click(view, 500, 1300)
    view.set_zones(zones, GEOM)
    assert view.selected_zone == ("no_go", 0)
    view.set_zones(MapZones(), GEOM)
    assert view.selected_zone is None
    view.set_zones(zones, GEOM)
    _click(view, 500, 1300)
    view.set_tool(None)
    assert view.selected_zone is None


def test_order_badges_number_visible_rooms():
    view = _view()
    assert view.order_badges() == {}
    view.set_order([3, 5, 1])  # room 5 is hidden
    assert view.order_badges() == {3: 1, 1: 3}
    view.set_order(None)
    assert view.order_badges() == {}


def test_snapshot_draws_zones_previews_handles_and_badges():
    zones = MapZones(
        no_go=(Rect(0, 1000, 1000, 1600),),
        no_mop=(Rect(-1000, 1000, 0, 1600),),
        walls=(Line(0, 600, 1000, 600),),
    )
    for tool in ("select", "wall", "no_mop"):
        view, _events = _edit_view(tool, zones)
        view.set_order([1, 2])
        _click(view, 500, 1300)
        x0, y0 = _at(view, 2000, 1200)
        view._on_drag_begin(None, x0, y0)
        view._on_drag_update(None, 40, 30)  # preview while dragging
        snapshot = Gtk.Snapshot()
        view.do_snapshot(snapshot)
        assert snapshot.to_node() is not None
    view, _events = _edit_view("select", zones, None)
    snapshot = Gtk.Snapshot()
    view.do_snapshot(snapshot)
    assert snapshot.to_node() is not None
