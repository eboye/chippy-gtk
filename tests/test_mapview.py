import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from chippy.core.fake import build_fake_payload  # noqa: E402
from chippy.core.mapdecode import decode_map  # noqa: E402
from chippy.mapmodel import build_rooms, grid_from_frame  # noqa: E402

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
