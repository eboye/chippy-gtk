"""io.github.eboye.Chippy.Vacuum on the app's own bus name, for the Shell extension."""

import logging
from collections.abc import Callable

from gi.repository import Gio, GLib

from .core.state import Snapshot

log = logging.getLogger(__name__)

INTERFACE = "io.github.eboye.Chippy.Vacuum"
OBJECT_PATH = "/io/github/eboye/Chippy"
XML = f"""
<node>
  <interface name="{INTERFACE}">
    <property name="State" type="s" access="read"/>
    <property name="Battery" type="y" access="read"/>
    <property name="Charging" type="b" access="read"/>
    <property name="Error" type="s" access="read"/>
    <property name="Suction" type="s" access="read"/>
    <property name="Water" type="s" access="read"/>
    <property name="HasWaterTank" type="b" access="read"/>
    <property name="Rooms" type="a(us)" access="read"/>
    <property name="CleanedArea" type="u" access="read"/>
    <property name="CleaningTime" type="u" access="read"/>
    <method name="Start"/>
    <method name="Pause"/>
    <method name="Stop"/>
    <method name="Dock"/>
    <method name="Locate"/>
    <method name="CleanRooms"><arg name="ids" type="au" direction="in"/></method>
    <method name="SetSuction"><arg name="level" type="s" direction="in"/></method>
    <method name="SetWater"><arg name="level" type="s" direction="in"/></method>
    <method name="RefreshRooms"/>
    <method name="ShowWindow"/>
  </interface>
</node>"""

_SNAPSHOT_PROPS = {
    "State": ("state", "s"),
    "Battery": ("battery", "y"),
    "Charging": ("charging", "b"),
    "Error": ("error", "s"),
    "Suction": ("suction", "s"),
    "Water": ("water", "s"),
    "HasWaterTank": ("has_water_tank", "b"),
    "CleanedArea": ("cleaned_area", "u"),
    "CleaningTime": ("cleaning_time", "u"),
}
_ACTIONS = {
    "Start": "start",
    "Pause": "pause",
    "Stop": "stop",
    "Dock": "dock",
    "Locate": "locate",
    "SetSuction": "set_suction",
    "SetWater": "set_water",
}
_FAILED = "org.freedesktop.DBus.Error.Failed"


class VacuumDBus:
    def __init__(self, show_window: Callable[[], None]):
        self._show_window = show_window
        self._controller = None
        self._signal_ids: list[int] = []
        self._connection: Gio.DBusConnection | None = None
        self._path = OBJECT_PATH
        self._reg_id = 0
        self._last: dict[str, GLib.Variant] = {}

    def register(self, connection: Gio.DBusConnection, path: str) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._connection, self._path = connection, path
        self._reg_id = connection.register_object(path, info, self._on_call, self._on_get, None)
        self._last = self._values()

    def unregister(self) -> None:
        if self._connection and self._reg_id:
            self._connection.unregister_object(self._reg_id)
        self._reg_id = 0
        self.attach(None)

    def attach(self, controller) -> None:
        if self._controller is not None:
            for sig in self._signal_ids:
                self._controller.disconnect(sig)
        self._signal_ids = []
        self._controller = controller
        if controller is not None:
            self._signal_ids = [
                controller.connect(s, lambda *_a: self._emit_changes())
                for s in ("snapshot-changed", "rooms-changed")
            ]
        self._emit_changes()

    # values ----------------------------------------------------------------
    def _values(self) -> dict[str, GLib.Variant]:
        snap = self._controller.snapshot if self._controller else Snapshot()
        values = {
            name: GLib.Variant(sig, getattr(snap, field))
            for name, (field, sig) in _SNAPSHOT_PROPS.items()
        }
        rooms = self._controller.visible_rooms() if self._controller else []
        values["Rooms"] = GLib.Variant("a(us)", [(r.id, r.name) for r in rooms])
        return values

    def _emit_changes(self) -> None:
        if not (self._connection and self._reg_id):
            return
        values = self._values()
        changed = {k: v for k, v in values.items() if self._last.get(k) != v}
        self._last = values
        if changed:
            self._connection.emit_signal(
                None,
                self._path,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, changed, [])),
            )

    def _on_get(self, _conn, _sender, _path, _iface, prop):
        return self._values().get(prop)

    # methods ---------------------------------------------------------------
    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            self._dispatch(method, params, invocation)
        except Exception as err:  # noqa: BLE001 - never leave an invocation unanswered
            log.exception("D-Bus method %s failed", method)
            invocation.return_dbus_error(_FAILED, str(err))

    def _dispatch(self, method, params, invocation):
        args = params.unpack() if params is not None else ()
        if method == "ShowWindow":
            self._show_window()
            invocation.return_value(None)
            return
        ctl = self._controller
        if ctl is None:
            invocation.return_dbus_error(_FAILED, "Not signed in")
            return

        def done(error):
            if error is None:
                invocation.return_value(None)
            else:
                invocation.return_dbus_error(_FAILED, str(error))

        if method == "RefreshRooms":
            ctl.refresh_map()
            invocation.return_value(None)
        elif method == "CleanRooms":
            ctl.clean_rooms(list(args[0]), on_done=done)
        elif method in _ACTIONS:
            ctl.act(_ACTIONS[method], *args, on_done=done)
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
