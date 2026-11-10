from dataclasses import replace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from chippy.core.mapedit import (  # noqa: E402
    DEFAULT_SETTING,
    EditSession,
    Line,
    MapZones,
    Rect,
)
from chippy.core.profiles import R2209  # noqa: E402
from chippy.ui.editpanel import NOT_SUPPORTED, SETTINGS_UNKNOWN, EditPanel  # noqa: E402


def _session(settings_known=True) -> EditSession:
    return EditSession(
        map_id=7,
        zones=MapZones(no_go=(Rect(0, 0, 1000, 1000),), walls=(Line(0, 0, 0, 900),)),
        names={1: "Living room", 2: "Kitchen", 3: "Hallway"},
        settings={1: DEFAULT_SETTING, 2: DEFAULT_SETTING, 3: DEFAULT_SETTING},
        order=[1, 2, 3],
        use_room_settings=False,
        settings_known=settings_known,
    )


def _panel(settings_known=True):
    window = Adw.Window()  # a root: dialogs show inside it (never as their own window)
    panel = EditPanel()
    window.set_content(panel)
    panel.build_actions()
    original = _session(settings_known)
    panel.set_session(original, original.copy(), R2209)
    events = []
    for name in ("changed", "leave"):
        panel.connect(name, lambda *_a, n=name: events.append(n))
    return panel, events


def test_settings_unknown_shows_the_info_label():
    panel, _ = _panel(settings_known=False)
    assert panel.info_label.get_visible()
    assert panel.info_label.get_label() == SETTINGS_UNKNOWN
    assert SETTINGS_UNKNOWN == "Settings from the robot aren't known yet; saving sets them."
    panel, _ = _panel(settings_known=True)
    assert not panel.info_label.get_visible()


def test_room_panel_follows_the_picked_room():
    panel, _ = _panel()
    assert not panel.room_group.get_visible()
    panel.pick_room(2)
    assert panel.room_group.get_visible()
    assert panel.room_group.get_title() == "Kitchen"
    assert panel.name_row.get_text() == "Kitchen"
    assert panel.passes_row.get_value() == 1
    assert panel.order_row.get_subtitle() == "2 of 3"


def test_changing_passes_marks_the_session_dirty_and_enables_save():
    panel, events = _panel()
    assert not panel.dirty
    assert not panel.save_button.get_sensitive()
    panel.pick_room(1)
    panel.passes_row.set_value(3)
    assert panel.edited.settings[1].passes == 3
    assert panel.original.settings[1].passes == 1
    assert panel.dirty
    assert panel.save_button.get_sensitive()
    assert "changed" in events


def test_save_is_insensitive_while_saving_or_when_not_allowed():
    panel, _ = _panel()
    panel.pick_room(1)
    panel.suction_row.set_selected(3)
    assert panel.edited.settings[1].suction == "turbo"
    assert panel.save_button.get_sensitive()
    panel.set_saving(True)
    assert not panel.save_button.get_sensitive()
    assert panel.save_spinner.get_visible()
    panel.set_saving(False)
    panel.set_allowed(False)
    assert not panel.save_button.get_sensitive()


def test_discard_changes_restores_the_original():
    panel, events = _panel()
    panel.pick_room(1)
    panel.name_row.set_text("Lounge")
    assert panel.dirty
    panel.cancel()
    assert panel.alert is not None
    assert panel.alert.get_heading() == "Discard changes?"
    panel.alert.emit("response", "keep")  # Keep editing: nothing changes
    assert panel.dirty and "leave" not in events
    panel.cancel()
    panel.alert.emit("response", "discard")
    assert not panel.dirty
    assert panel.edited.names == panel.original.names
    assert panel.edited is not panel.original
    assert events[-1] == "leave"


def test_cancel_without_changes_leaves_at_once():
    panel, events = _panel()
    panel.cancel()
    assert panel.alert is None
    assert events == ["leave"]


def test_order_picks_build_a_new_sequence_and_reset_restores_it():
    panel, _ = _panel()
    panel.set_tool("order")
    assert panel.reset_order_button.get_visible()
    panel.pick_room(3)
    assert panel.edited.order == [3, 1, 2]
    panel.pick_room(2)
    assert panel.edited.order == [3, 2, 1]
    panel.pick_room(3)  # already placed: no change
    assert panel.edited.order == [3, 2, 1]
    assert panel.order_row.get_subtitle() == "1 of 3"
    panel.set_tool("select")
    panel.set_tool("order")  # a new Order pass starts a fresh sequence
    panel.pick_room(1)
    assert panel.edited.order == [1, 3, 2]
    panel.reset_order_button.emit("clicked")
    assert panel.edited.order == [1, 2, 3]
    assert not panel.dirty
    panel.set_tool("select")
    assert not panel.reset_order_button.get_visible()


def test_selected_zone_row_deletes_the_zone():
    panel, events = _panel()
    assert not panel.zone_group.get_visible()
    panel.set_zone(("wall", 0))
    assert panel.zone_group.get_visible()
    assert panel.zone_row.get_title() == "Wall"
    panel.zone_delete.emit("clicked")
    assert panel.edited.zones.walls == ()
    assert panel.edited.zones.no_go == panel.original.zones.no_go
    assert not panel.zone_group.get_visible()
    assert panel.dirty and "changed" in events


def test_use_room_settings_switch():
    panel, _ = _panel()
    panel.room_settings_row.set_active(True)
    assert panel.edited.use_room_settings
    assert panel.dirty


def test_unsupported_kinds_disable_their_controls_and_revert_them():
    panel, _ = _panel()
    panel.pick_room(1)
    panel.name_row.set_text("Lounge")
    panel.passes_row.set_value(2)
    panel.edited.unsupported.add("names")
    panel.apply_unsupported()
    assert not panel.name_row.get_sensitive()
    assert panel.name_row.get_tooltip_text() == NOT_SUPPORTED == "Not supported on this model"
    assert panel.edited.names == panel.original.names  # a failing kind is not resent
    assert panel.edited.settings[1].passes == 2
    assert panel.passes_row.get_sensitive()
    assert panel.name_row.get_text() == "Living room"


def test_new_session_keeps_the_zone_edits():
    panel, _ = _panel()
    zones = MapZones(no_mop=(Rect(0, 0, 500, 500),))
    panel.set_zones(zones)
    fresh = _session()
    panel.set_session(fresh, fresh.copy(), R2209, keep_zones=panel.edited.zones)
    assert panel.edited.zones == zones
    assert panel.original.zones == fresh.zones
    assert panel.dirty


class _SidebarController:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def notices(self):
        return []

    def visible_rooms(self):
        return []


def test_sidebar_levels_say_per_room_when_room_settings_are_on():
    from chippy.core.state import Snapshot
    from chippy.ui.sidebar import Sidebar

    sidebar = Sidebar()
    sidebar.build_actions()
    snap = Snapshot(state="docked", suction="strong", water="high", has_water_tank=True)
    sidebar.update(_SidebarController(replace(snap, room_settings=True)), set())
    for row in (sidebar.suction_row, sidebar.water_row):
        assert row.get_subtitle() == "Per room"
        assert not row.get_sensitive()
    sidebar.update(_SidebarController(replace(snap, room_settings=False)), set())
    for row in (sidebar.suction_row, sidebar.water_row):
        assert not row.get_subtitle()
        assert row.get_sensitive()


def test_loading_state():
    panel, _ = _panel()
    panel.begin()
    assert panel.edited is None
    assert panel.content_stack.get_visible_child_name() == "loading"
    assert not panel.save_button.get_sensitive()
