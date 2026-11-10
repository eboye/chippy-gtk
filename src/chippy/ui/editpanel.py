"""The sidebar in edit mode: the staged map edit (zones, room names and settings, cleaning
order, the "Use room settings" switch) with Cancel / Save to robot."""

from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402

from ..core.mapedit import EditSession, MapZones, RoomSetting  # noqa: E402
from .sidebar import SUCTION, WATER  # noqa: E402

SETTINGS_UNKNOWN = "Settings from the robot aren't known yet; saving sets them."
NOT_SUPPORTED = "Not supported on this model"
ZONE_TITLES = {"no_go": "No-go zone", "no_mop": "No-mop zone", "wall": "Wall"}
_ZONE_FIELD = {"no_go": "no_go", "no_mop": "no_mop", "wall": "walls"}


def confirm(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    accept: tuple[str, str],
    on_accept,
    cancel: tuple[str, str] = ("cancel", "Cancel"),
    destructive: bool = True,
    on_close=None,
) -> Adw.AlertDialog:
    """Ask before doing something; Return picks `accept` (the probe relies on it)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response(*cancel)
    dialog.add_response(*accept)
    dialog.set_response_appearance(
        accept[0],
        Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response(accept[0])
    dialog.set_close_response(cancel[0])

    def responded(_dialog, response):
        if on_close is not None:
            on_close()
        if response == accept[0]:
            on_accept()

    dialog.connect("response", responded)
    dialog.present(parent)
    return dialog


def mark_unsupported(widget: Gtk.Widget, unsupported: bool) -> None:
    widget.set_sensitive(not unsupported)
    widget.set_tooltip_text(NOT_SUPPORTED if unsupported else None)


class EditPanel(Gtk.Box):
    """Edits mutate `edited` (never `original`) and emit `changed`; the window redraws the
    map from `edited`. `leave` means the user is done (nothing to keep, or discarded)."""

    __gtype_name__ = "ChippyEditPanel"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "leave": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "save": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.original: EditSession | None = None
        self.edited: EditSession | None = None
        self.profile = None
        self.room = 0  # the room shown in the room panel
        self.zone: tuple[str, int] | None = None
        self.tool: str | None = None
        self.alert: Adw.AlertDialog | None = None
        self._picks: list[int] = []  # rooms clicked so far in this Order pass
        self._saving = False
        self._allowed = True
        self._updating = False

        self.content_stack = Gtk.Stack(vexpand=True)
        loading = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, vexpand=True)
        loading.append(Adw.Spinner(width_request=32, height_request=32))
        self.content_stack.add_named(loading, "loading")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=6,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.content_stack.add_named(body, "session")
        self.append(self.content_stack)

        self.info_label = Gtk.Label(
            label=SETTINGS_UNKNOWN, wrap=True, xalign=0, css_classes=["dim-label"], visible=False
        )
        body.append(self.info_label)

        general = Adw.PreferencesGroup()
        self.room_settings_row = Adw.SwitchRow(title="Use room settings")
        self.room_settings_row.connect("notify::active", self._on_room_settings)
        general.add(self.room_settings_row)
        body.append(general)

        self.zone_group = Adw.PreferencesGroup(visible=False)
        self.zone_row = Adw.ActionRow()
        self.zone_delete = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text="Delete",
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        self.zone_delete.update_property([Gtk.AccessibleProperty.LABEL], ["Delete"])
        self.zone_delete.connect("clicked", lambda *_a: self.delete_zone())
        self.zone_row.add_suffix(self.zone_delete)
        self.zone_group.add(self.zone_row)
        body.append(self.zone_group)

        self.room_hint = Gtk.Label(
            label="Click a room on the map to change its name and settings.",
            wrap=True,
            xalign=0,
            css_classes=["dim-label"],
        )
        body.append(self.room_hint)
        self.room_group = Adw.PreferencesGroup(visible=False)
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.connect("changed", self._on_name)
        self.suction_row = Adw.ComboRow(
            title="Suction", model=Gtk.StringList.new([s.title() for s in SUCTION])
        )
        self.suction_row.connect("notify::selected", self._on_setting)
        self.water_row = Adw.ComboRow(
            title="Mopping", model=Gtk.StringList.new([w.title() for w in WATER])
        )
        self.water_row.connect("notify::selected", self._on_setting)
        self.passes_row = Adw.SpinRow.new_with_range(1, 3, 1)
        self.passes_row.set_title("Passes")
        self.passes_row.set_digits(0)
        self.passes_row.connect("notify::value", self._on_setting)
        self.order_row = Adw.ActionRow(title="Cleaning order")
        for row in (
            self.name_row,
            self.suction_row,
            self.water_row,
            self.passes_row,
            self.order_row,
        ):
            self.room_group.add(row)
        body.append(self.room_group)

        self.reset_order_button = Gtk.Button(
            label="Reset order", halign=Gtk.Align.START, css_classes=["pill"], visible=False
        )
        self.reset_order_button.connect("clicked", lambda *_a: self.reset_order())
        body.append(self.reset_order_button)

    def build_actions(self) -> Gtk.Box:
        """Bottom bar (placed by the window in the sidebar's ToolbarView)."""
        bar = Gtk.Box(
            spacing=10,
            homogeneous=True,
            margin_top=10,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.cancel_button = Gtk.Button(label="Cancel", css_classes=["pill"])
        self.cancel_button.connect("clicked", lambda *_a: self.cancel())
        self.save_button = Gtk.Button(css_classes=["pill", "suggested-action"], sensitive=False)
        content = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.save_spinner = Adw.Spinner(visible=False)
        content.append(self.save_spinner)
        content.append(Gtk.Label(label="Save to robot"))
        self.save_button.set_child(content)
        self.save_button.update_property([Gtk.AccessibleProperty.LABEL], ["Save to robot"])
        self.save_button.connect("clicked", lambda *_a: self.emit("save"))
        bar.append(self.cancel_button)
        bar.append(self.save_button)
        return bar

    # state -------------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        if self.edited is None or self.original is None:
            return False
        return bool(self.edited.changes(self.original, self.profile))

    @property
    def saving(self) -> bool:
        return self._saving

    def begin(self) -> None:
        """Waiting for a session (entering edit mode, or after a merge or split)."""
        self.original = self.edited = None
        self.room, self.zone, self._picks = 0, None, []
        self.content_stack.set_visible_child_name("loading")
        self._update_actions()

    def set_session(
        self,
        original: EditSession,
        edited: EditSession,
        profile,
        keep_zones: MapZones | None = None,
    ) -> None:
        """Show a session; `keep_zones` re-applies unsaved zone edits onto a fresh one."""
        self.original, self.edited, self.profile = original, edited, profile
        if keep_zones is not None:
            edited.zones = keep_zones
        if self.room not in edited.names:
            self.room = 0
        self.zone, self._picks = None, []
        self.content_stack.set_visible_child_name("session")
        self.info_label.set_visible(not original.settings_known)
        self.apply_unsupported()

    def set_tool(self, tool: str | None) -> None:
        self.tool = tool
        self._picks = []
        self.reset_order_button.set_visible(tool == "order")

    def set_saving(self, saving: bool) -> None:
        self._saving = saving
        self.save_spinner.set_visible(saving)
        self.content_stack.set_sensitive(not saving)
        self._update_actions()

    def set_allowed(self, allowed: bool) -> None:
        """Whether the robot accepts edits now (not while cleaning, offline, ...)."""
        self._allowed = allowed
        self._update_actions()

    def apply_unsupported(self) -> None:
        """Disable the controls of kinds the robot refused and drop their staged changes, so
        the next save does not stop at them again."""
        edited, original = self.edited, self.original
        if edited is None:
            return
        off = edited.unsupported
        if "zones" in off:
            edited.zones = original.zones
        if "names" in off:
            edited.names = dict(original.names)
        if "settings" in off:
            edited.settings = dict(original.settings)
        if "order" in off:
            edited.order = list(original.order)
        if "room_settings" in off:
            edited.use_room_settings = original.use_room_settings
        mark_unsupported(self.name_row, "names" in off)
        for row in (self.suction_row, self.water_row, self.passes_row):
            mark_unsupported(row, "settings" in off)
        mark_unsupported(self.room_settings_row, "room_settings" in off)
        mark_unsupported(self.reset_order_button, "order" in off)
        mark_unsupported(self.zone_delete, "zones" in off)
        self._sync()

    # edits -------------------------------------------------------------------
    def pick_room(self, seg: int) -> None:
        """A room clicked with Select (show it) or Order (also place it next)."""
        edited = self.edited
        if edited is None or seg not in edited.names:
            return
        self.room = seg
        if self.tool == "order" and seg not in self._picks and "order" not in edited.unsupported:
            self._picks.append(seg)
            edited.order = self._picks + [s for s in edited.order if s not in self._picks]
            self._changed()
            return
        self._sync()

    def reset_order(self) -> None:
        if self.edited is None:
            return
        self.edited.order = list(self.original.order)
        self._picks = []
        self._changed()

    def set_zone(self, zone: tuple[str, int] | None) -> None:
        self.zone = zone
        self._sync()

    def set_zones(self, zones: MapZones) -> None:
        if self.edited is None:
            return
        self.edited.zones = zones
        self._changed()

    def delete_zone(self) -> None:
        if self.edited is None or self.zone is None:
            return
        kind, index = self.zone
        field = _ZONE_FIELD[kind]
        items = list(getattr(self.edited.zones, field))
        if 0 <= index < len(items):
            del items[index]
            self.edited.zones = replace(self.edited.zones, **{field: tuple(items)})
        self.zone = None
        self._changed()

    def cancel(self) -> None:
        """Cancel (or the Edit toggle switched off): ask first when there are changes."""
        if self._saving or self.alert is not None:
            return
        if not self.dirty:
            self.emit("leave")
            return

        def closed():
            self.alert = None

        self.alert = confirm(
            self,
            "Discard changes?",
            "Your edits haven't been saved to the robot.",
            ("discard", "Discard"),
            self.discard,
            cancel=("keep", "Keep editing"),
            on_close=closed,
        )

    def discard(self) -> None:
        if self.original is not None:
            self.edited = self.original.copy()
            self._sync()
        self.emit("leave")

    def _on_room_settings(self, row, _pspec) -> None:
        if self._updating or self.edited is None:
            return
        self.edited.use_room_settings = row.get_active()
        self._changed()

    def _on_name(self, row) -> None:
        if self._updating or self.edited is None or not self.room:
            return
        self.edited.names[self.room] = row.get_text()
        self._changed()

    def _on_setting(self, *_args) -> None:
        if self._updating or self.edited is None or not self.room:
            return
        suction, water = self.suction_row.get_selected(), self.water_row.get_selected()
        if suction >= len(SUCTION) or water >= len(WATER):
            return
        self.edited.settings[self.room] = RoomSetting(
            SUCTION[suction], WATER[water], int(self.passes_row.get_value())
        )
        self._changed()

    def _changed(self) -> None:
        self._sync()
        self.emit("changed")

    # widgets -----------------------------------------------------------------
    def _sync(self) -> None:
        """Show `edited` in the widgets (without feeding the changes back)."""
        self._updating = True
        try:
            self._sync_widgets()
        finally:
            self._updating = False
        self._update_actions()

    def _sync_widgets(self) -> None:
        edited = self.edited
        if edited is None:
            return
        self.room_settings_row.set_active(edited.use_room_settings)
        items = getattr(edited.zones, _ZONE_FIELD[self.zone[0]]) if self.zone else ()
        if self.zone is not None and self.zone[1] < len(items):
            self.zone_row.set_title(ZONE_TITLES[self.zone[0]])
            self.zone_group.set_visible(True)
        else:
            self.zone = None
            self.zone_group.set_visible(False)
        seg = self.room
        self.room_group.set_visible(bool(seg))
        self.room_hint.set_visible(not seg)
        if not seg:
            return
        name = edited.names.get(seg, "")
        self.room_group.set_title(name.strip() or self.original.names.get(seg, ""))
        if self.name_row.get_text() != name:
            self.name_row.set_text(name)
        setting = edited.settings.get(seg)
        if setting is not None:
            if setting.suction in SUCTION:
                self.suction_row.set_selected(SUCTION.index(setting.suction))
            if setting.water in WATER:
                self.water_row.set_selected(WATER.index(setting.water))
            self.passes_row.set_value(setting.passes)
        if seg in edited.order:
            self.order_row.set_subtitle(f"{edited.order.index(seg) + 1} of {len(edited.order)}")
        else:
            self.order_row.set_subtitle("")

    def _update_actions(self) -> None:
        if not hasattr(self, "save_button"):
            return
        self.save_button.set_sensitive(self.dirty and not self._saving and self._allowed)
        self.cancel_button.set_sensitive(not self._saving)
