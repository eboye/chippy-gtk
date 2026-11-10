"""Edit mode in a real MainWindow with a real controller on the fake robot.

Run by tests/test_edit_window.py in its own process on a private session bus: a GtkApplication
must be registered before it gets windows, and that must never reach the user's session."""

import os
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from chippy.controller import VacuumController  # noqa: E402
from chippy.core.fake import FakeMap, FakeVacuum, build_fake_payload  # noqa: E402
from chippy.core.mapedit import Line, MapZones, Rect  # noqa: E402
from chippy.core.profiles import R2209  # noqa: E402
from chippy.store import MapCache, RoomStore  # noqa: E402
from tests.test_controller import InlineRunner, ManualScheduler  # noqa: E402


class StubApp(Adw.Application):
    __gsignals__ = {"state-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__(
            application_id="io.github.eboye.ChippyUnitTest",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.state = "loading"
        self.controller = None

    def primary_menu(self):
        return Gio.Menu()

    def retry_keyring(self):
        pass


def build(tmp: Path):
    from chippy.ui.window import MainWindow

    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    ctl = VacuumController(
        vac,
        R2209,
        RoomStore(tmp / "rooms.json", legacy=tmp / "none.json"),
        MapCache(tmp / "map.payload"),
        lambda: build_fake_payload(fake_map=fm),
        runner=InlineRunner(),
        scheduler=ManualScheduler(),
        fetch_cleanset=lambda: fm.cleanset,
    )
    app = StubApp()
    assert app.register(None)
    ctl.start()
    app.state, app.controller = "ready", ctl
    window = MainWindow(app)
    toasts = []
    window.toast = toasts.append
    return window, ctl, vac, fm, toasts


def edit_state(window):
    return window.test_state()["edit"]


def enter(window):
    window.edit_button.set_active(True)
    assert window.editing and window.edit_panel.edited is not None


def respond(alert, response):
    alert.emit("response", response)


def check_save(window, ctl, fm, toasts):
    assert window.edit_button.get_sensitive()
    assert edit_state(window)["mode"] is False
    enter(window)
    assert edit_state(window)["tool"] == "select"
    assert window.side_stack.get_visible_child_name() == "edit"
    assert window.side_actions.get_visible_child_name() == "edit"
    assert window.toolbar.get_visible()
    assert not window.edit_panel.save_button.get_sensitive()
    zones = MapZones(no_go=(Rect(1000, 1000, 2000, 2000),), walls=(Line(0, 0, 0, 900),))
    window.map_view.emit("zones-edited", zones)
    state = edit_state(window)
    assert state["dirty"] and state["zones"] == {"no_go": 1, "no_mop": 0, "walls": 1}
    assert window.edit_panel.save_button.get_sensitive()
    window.edit_panel.save_button.emit("clicked")
    assert toasts[-1] == "Saved to robot"
    assert not window.editing and not window.edit_button.get_active()
    assert window.side_stack.get_visible_child_name() == "controls"
    assert fm.zones == zones
    # outside edit mode the map shows the zones the robot sent back
    assert edit_state(window)["zones"] == {"no_go": 1, "no_mop": 0, "walls": 1}
    assert window.map_view.tool is None and window.map_view.order is None


def check_discard(window):
    enter(window)
    window.map_view.emit("room-picked", 2)
    assert window.edit_panel.room == 2
    assert window.map_view.selected == {2}
    window.edit_panel.passes_row.set_value(3)
    assert edit_state(window)["dirty"]
    window.edit_button.set_active(False)  # toggling off with changes asks first
    alert = window.edit_panel.alert
    assert alert is not None and alert.get_heading() == "Discard changes?"
    assert window.editing and window.edit_button.get_active()
    shown = edit_state(window)["alert"]
    assert shown["heading"] == "Discard changes?", shown
    assert set(shown["buttons"]) == {"keep", "discard"}, shown
    assert alert.get_default_response() == "keep"  # Return never discards
    respond(alert, "keep")
    assert window.editing and edit_state(window)["dirty"]
    window.edit_panel.cancel_button.emit("clicked")
    respond(window.edit_panel.alert, "discard")
    assert not window.editing and not window.edit_button.get_active()
    assert window.map_view.selected == set()


def check_leaves_when_the_robot_gets_busy(window, ctl, vac):
    enter(window)  # no changes: leaves at once
    ctl.act("start")
    assert ctl.snapshot.state == "cleaning"
    assert not window.editing
    assert not window.edit_button.get_sensitive()
    ctl.act("stop")
    assert window.edit_button.get_sensitive()
    enter(window)  # with changes: asks first
    window.map_view.emit("zones-edited", MapZones())
    ctl.act("start")
    assert window.editing and window.edit_panel.alert is not None
    assert not window.edit_panel.save_button.get_sensitive()
    respond(window.edit_panel.alert, "discard")
    assert not window.editing
    ctl.act("stop")


def check_merge_and_split(window, ctl, fm, toasts):
    enter(window)
    panel = window.edit_panel
    extra = MapZones(no_mop=(Rect(0, 0, 600, 600),))
    window.map_view.emit("zones-edited", extra)
    # unsaved edits that must survive the merge (for rooms that still exist)
    window.map_view.emit("room-picked", 1)
    panel.name_row.set_text("Lounge")
    window.map_view.emit("room-picked", 2)
    panel.passes_row.set_value(3)
    window.map_view.emit("room-picked", 4)
    panel.name_row.set_text("Gone")  # room 4 is merged away: dropped
    panel.room_settings_row.set_active(True)
    window.tool_buttons["order"].set_active(True)
    window.map_view.emit("room-picked", 4)
    window.map_view.emit("room-picked", 3)
    assert panel.edited.order == [4, 3, 1, 2]
    window.tool_buttons["merge"].set_active(True)
    assert edit_state(window)["tool"] == "merge"
    window.map_view.emit("room-picked", 2)
    assert window.map_view.selected == {2}
    window.map_view.emit("room-picked", 4)
    alert = window._alert
    assert alert is not None and alert.get_heading() == "Merge Kitchen and Gone?"
    respond(alert, "merge")
    assert 4 not in fm.ids()
    assert window.editing and window.edit_panel.edited is not None
    assert 4 not in window.edit_panel.edited.names
    assert window.edit_panel.edited.zones == extra  # unsaved zone edits survive
    edited = panel.edited
    assert edited.names[1] == "Lounge" and panel.original.names[1] == "Living room"
    assert edited.settings[2].passes == 3
    assert edited.order == [3, 1, 2]
    assert edited.use_room_settings is True
    assert window.edit_panel.dirty
    window.tool_buttons["split"].set_active(True)
    window.map_view.emit("room-picked", 1)
    rooms_before = len(fm.ids())
    window.map_view.emit("split-line", 99, Line(0, 0, 900, 900))  # no such room: refused
    respond(window._alert, "split")
    assert any("Split failed" in t for t in toasts), toasts
    assert window.edit_panel.edited.zones == extra and len(fm.ids()) == rooms_before
    xs = sorted({x for row in fm.layout for x in range(len(row)) if row[x] == "1"})
    ys = sorted({y for y, row in enumerate(fm.layout) if "1" in row})
    from chippy.core.fake import _cell_mm

    mid_x = (xs[0] + xs[-1] + 1) / 2
    top, bottom = _cell_mm(mid_x, ys[0] - 1), _cell_mm(mid_x, ys[-1] + 2)
    window.map_view.emit("split-line", 1, Line(*top, *bottom))
    alert = window._alert
    assert alert.get_heading() == "Split Lounge?"  # the unsaved name carried over
    respond(alert, "split")
    assert len(fm.ids()) == rooms_before + 1
    assert window.editing and window.edit_panel.edited.zones == extra
    new_room = max(fm.ids())
    assert panel.edited.names[1] == "Lounge"
    assert panel.edited.order == [3, 1, 2, new_room]  # the new room after the survivors
    window.edit_panel.cancel()
    respond(window.edit_panel.alert, "discard")
    assert not window.editing


def check_unsupported(window, ctl, vac, toasts):
    enter(window)
    original_update = vac.update_map

    def refuse_order(payload):
        if "cleanOrder" in payload:
            from chippy.core.device import DeviceError

            raise DeviceError("refused")
        original_update(payload)

    vac.update_map = refuse_order
    window.tool_buttons["order"].set_active(True)
    window.map_view.emit("room-picked", 3)
    assert window.edit_panel.edited.order[0] == 3
    window.edit_panel.save_button.emit("clicked")
    assert window.editing and toasts[-1].startswith("Saving failed")
    order_button = window.tool_buttons["order"]
    assert not order_button.get_sensitive()
    assert order_button.get_tooltip_text() == "Not supported on this model"
    assert window.map_view.tool == "select"
    assert not window.edit_panel.dirty  # the refused change was dropped
    vac.update_map = original_update
    window.edit_panel.cancel()
    assert not window.editing


def check_zones_read_only(window, vac, toasts):
    from chippy.core.device import DeviceError

    enter(window)
    original_update = vac.update_map

    def refuse_zones(payload):
        if "vw" in payload:
            raise DeviceError("refused")
        original_update(payload)

    vac.update_map = refuse_zones
    staged = window.edit_panel.edited.zones
    window.map_view.emit("zones-edited", MapZones(no_go=(Rect(0, 0, 900, 900),)))
    window.edit_panel.save_button.emit("clicked")
    assert toasts[-1].startswith("Saving failed")
    assert window.edit_panel.edited.zones == staged
    assert not window.map_view.zones_editable
    assert not window.tool_buttons["no_go"].get_sensitive()
    # a map edit that slips through is ignored and the map goes back to the staged zones
    window.map_view.zones = MapZones(walls=(Line(0, 0, 0, 900),))
    window.map_view.emit("zones-edited", window.map_view.zones)
    assert window.edit_panel.edited.zones == staged and window.map_view.zones == staged
    assert not window.edit_panel.dirty
    vac.update_map = original_update
    window.edit_panel.cancel()
    assert not window.editing
    enter(window)  # a new session: zones are editable again
    assert window.map_view.zones_editable
    window.edit_panel.cancel()


def check_save_freezes_the_toggle_and_leaves_after(window, ctl, vac, toasts):
    enter(window)
    window.map_view.emit("zones-edited", MapZones(no_go=(Rect(0, 0, 900, 900),)))
    original_update = vac.update_map
    seen = {}

    def busy_then_fail(payload):
        ctl.poke()  # a snapshot refresh during the save (robot still docked)
        seen["toggle_while_docked"] = window.edit_button.get_sensitive()
        vac.start()  # someone starts a clean from the phone
        ctl.poke()
        seen["editing"], seen["alert"] = window.editing, window.edit_panel.alert
        raise OSError("timed out")

    vac.update_map = busy_then_fail
    window.edit_panel.save_button.emit("clicked")
    vac.update_map = original_update
    assert seen == {"toggle_while_docked": False, "editing": True, "alert": None}, seen
    assert toasts[-1] == "Saving failed: timed out"
    # the save is over, the robot is busy and there are changes: ask now
    alert = window.edit_panel.alert
    assert alert is not None and alert.get_heading() == "Discard changes?"
    respond(alert, "discard")
    assert not window.editing
    ctl.act("stop")


def check_no_stacked_dialogs(window, ctl):
    enter(window)
    window.map_view.emit("zones-edited", MapZones(no_go=(Rect(0, 0, 900, 900),)))
    window.tool_buttons["merge"].set_active(True)
    window.map_view.emit("room-picked", 1)
    window.map_view.emit("room-picked", 2)
    assert window._alert is not None
    ctl.act("start")
    assert window._alert is None  # the merge question is gone ...
    alert = window.edit_panel.alert  # ... replaced by the discard question
    assert alert is not None and alert.get_heading() == "Discard changes?"
    assert window.get_visible_dialog() in (None, alert)
    respond(alert, "discard")
    assert not window.editing
    ctl.act("stop")


def main() -> int:
    if not Gtk.init_check():
        print("no display", file=sys.stderr)
        return 3
    Adw.init()
    with tempfile.TemporaryDirectory() as tmp:
        window, ctl, vac, fm, toasts = build(Path(tmp))
        check_save(window, ctl, fm, toasts)
        check_discard(window)
        check_leaves_when_the_robot_gets_busy(window, ctl, vac)
        check_merge_and_split(window, ctl, fm, toasts)
        check_unsupported(window, ctl, vac, toasts)
        check_zones_read_only(window, vac, toasts)
        check_save_freezes_the_toggle_and_leaves_after(window, ctl, vac, toasts)
        check_no_stacked_dialogs(window, ctl)
        ctl.stop()
        window.detach()
    print("ok")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GSETTINGS_BACKEND", "memory")
    sys.exit(main())
