"""Right-hand panel: status, rooms, settings, actions."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from ..mapmodel import PALETTE  # noqa: E402

STATE_LABELS = {
    "idle": "Idle",
    "cleaning": "Cleaning",
    "paused": "Paused",
    "returning": "Returning to dock",
    "charging": "Charging",
    "docked": "Docked",
    "error": "Needs help",
    "offline": "Offline",
}
STATE_ICONS = {
    "cleaning": "media-playback-start-symbolic",
    "paused": "media-playback-pause-symbolic",
    "returning": "go-home-symbolic",
    "charging": "battery-level-100-charging-symbolic",
    "docked": "battery-level-100-charged-symbolic",
    "error": "dialog-warning-symbolic",
    "offline": "network-offline-symbolic",
    "idle": "emblem-ok-symbolic",
}
SUCTION = ["quiet", "standard", "strong", "turbo"]
WATER = ["low", "medium", "high"]


NOTICE_ICONS = {
    "reminder": "dialog-information-symbolic",
    "attention": "dialog-warning-symbolic",
    "error": "dialog-error-symbolic",
}
# severity -> libadwaita colour name (its --<name>-color / --<name>-bg-color variables)
_NOTICE_COLORS = {"reminder": "accent", "attention": "warning", "error": "error"}


def room_dot_css() -> str:
    """App-wide CSS: one class per palette colour, so the list dots match the map, and the
    status card's notice pills."""
    dots = "".join(
        f".room-dot-{i} {{ background-color: {color}; border-radius: 4px; }}\n"
        for i, color in enumerate(PALETTE)
    )
    pills = ".notice-pill { border-radius: 999px; padding: 4px 12px 4px 8px; }\n" + "".join(
        f".notice-pill.notice-{severity} {{ color: var(--{name}-color);"
        f" background-color: color-mix(in srgb, var(--{name}-bg-color) 16%, transparent); }}\n"
        for severity, name in _NOTICE_COLORS.items()
    )
    return dots + pills


def notice_pill(severity: str, text: str) -> Gtk.Widget:
    pill = Gtk.Box(
        spacing=6, halign=Gtk.Align.START, css_classes=["notice-pill", f"notice-{severity}"]
    )
    pill.append(Gtk.Image(icon_name=NOTICE_ICONS.get(severity, NOTICE_ICONS["error"])))
    pill.append(
        # ask for one line (natural width unwrapped); wrap only when the card is narrower
        Gtk.Label(
            label=text,
            xalign=0,
            wrap=True,
            natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
            css_classes=["caption-heading"],
        )
    )
    pill.set_tooltip_text(text)
    return pill


def room_dot_class(color: str) -> str:
    return f"room-dot-{PALETTE.index(color)}" if color in PALETTE else "room-dot-0"


class Sidebar(Gtk.Box):
    __gtype_name__ = "ChippySidebar"
    __gsignals__ = {
        "toggle-room": (GObject.SignalFlags.RUN_FIRST, None, (int, bool)),
        "select-all": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "clean": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "level": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),  # ("suction"|"water", name)
        "action": (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # start/pause/stop/dock/locate
    }

    def __init__(self):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=6,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._updating = False
        self._state = "offline"
        self._rooms_key: list | None = None
        self.checks: dict[int, Gtk.CheckButton] = {}

        # status card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card"])
        top = Gtk.Box(spacing=12, margin_top=14, margin_start=14, margin_end=14)
        self.state_icon = Gtk.Image(pixel_size=32, css_classes=["accent"])
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.state_title = Gtk.Label(xalign=0, css_classes=["title-3"])
        self.state_sub = Gtk.Label(xalign=0, css_classes=["dim-label"], wrap=True)
        titles.append(self.state_title)
        titles.append(self.state_sub)
        top.append(self.state_icon)
        top.append(titles)
        self.battery = Gtk.LevelBar(min_value=0, max_value=100, margin_start=14, margin_end=14)
        self.last_clean = Gtk.Label(
            xalign=0, css_classes=["caption", "dim-label"], margin_start=14, margin_bottom=12
        )
        # the robot's current messages, one pill each (hidden when there are none)
        self.notices = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_start=14,
            margin_end=14,
            visible=False,
        )
        self._notices_key: list = []
        card.append(top)
        card.append(self.notices)
        card.append(self.battery)
        card.append(self.last_clean)
        self.append(card)

        # rooms
        self.rooms_group = Adw.PreferencesGroup(title="Rooms")
        select_all = Gtk.Button(label="Select all", css_classes=["flat"])
        select_all.connect("clicked", lambda *_a: self.emit("select-all"))
        self.rooms_group.set_header_suffix(select_all)
        self.rooms_list = Gtk.ListBox(
            css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE
        )
        self.rooms_group.add(self.rooms_list)
        self.append(self.rooms_group)

        # settings
        settings = Adw.PreferencesGroup()
        self.suction_row = Adw.ComboRow(
            title="Suction", model=Gtk.StringList.new([s.title() for s in SUCTION])
        )
        self.suction_row.connect("notify::selected", self._on_level, "suction", SUCTION)
        self.water_row = Adw.ComboRow(
            title="Mopping", model=Gtk.StringList.new([w.title() for w in WATER])
        )
        self.water_row.connect("notify::selected", self._on_level, "water", WATER)
        self.no_tank_row = Adw.ActionRow(
            title="Mopping", subtitle="No water tank fitted", sensitive=False
        )
        for row in (self.suction_row, self.water_row, self.no_tank_row):
            settings.add(row)
        self.append(settings)

    def build_actions(self) -> Gtk.Box:
        """Bottom bar (placed by the window in the ToolbarView)."""
        bar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=10,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.clean_button = Gtk.Button(
            label="Select rooms to clean",
            sensitive=False,
            css_classes=["suggested-action", "pill"],
        )
        self.clean_button.connect("clicked", lambda *_a: self.emit("clean"))
        bar.append(self.clean_button)
        row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.action_buttons: dict[str, Gtk.Button] = {}
        for name, icon, tip in (
            ("start", "media-playback-start-symbolic", "Start"),
            ("stop", "media-playback-stop-symbolic", "Stop"),
            ("dock", "go-home-symbolic", "Return to dock"),
            ("locate", "find-location-symbolic", "Locate"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["circular", "flat"])
            button.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            button.connect("clicked", lambda _b, n=name: self.emit("action", self._resolve(n)))
            self.action_buttons[name] = button
            row.append(button)
        bar.append(row)
        return bar

    def _resolve(self, name: str) -> str:
        if name == "start" and self._state == "cleaning":
            return "pause"
        return name

    def _on_level(self, row, _pspec, kind, names) -> None:
        if not self._updating and row.get_selected() < len(names):
            self.emit("level", kind, names[row.get_selected()])

    def _update_notices(self, notices: list[tuple[str, str]]) -> None:
        if notices == self._notices_key:
            return  # unchanged: keep the widgets (and any open tooltip)
        self._notices_key = list(notices)
        while child := self.notices.get_first_child():
            self.notices.remove(child)
        for severity, text in notices:
            self.notices.append(notice_pill(severity, text))
        self.notices.set_visible(bool(notices))

    def update(self, controller, selected: set[int]) -> None:
        self._updating = True
        try:
            snap = controller.snapshot
            self._state = snap.state
            self.state_title.set_label(STATE_LABELS.get(snap.state, snap.state.title()))
            sub = f"Battery {snap.battery}%"
            if snap.state in ("docked", "charging") and snap.battery >= 100:
                sub = "Charging complete · 100%"
            self.state_sub.set_label(sub)
            self._update_notices(controller.notices())
            self.state_icon.set_from_icon_name(STATE_ICONS.get(snap.state, "emblem-ok-symbolic"))
            self.battery.set_value(snap.battery)
            self.last_clean.set_label(
                f"Last clean {snap.cleaned_area} m² · {snap.cleaning_time} min"
            )
            usable = snap.state != "offline"
            if snap.suction in SUCTION:
                self.suction_row.set_selected(SUCTION.index(snap.suction))
            if snap.water in WATER:
                self.water_row.set_selected(WATER.index(snap.water))
            self.suction_row.set_sensitive(usable)
            self.water_row.set_sensitive(usable)
            self.water_row.set_visible(snap.has_water_tank)
            self.no_tank_row.set_visible(not snap.has_water_tank)
            visible = controller.visible_rooms()
            self._update_rooms(visible, selected)
            rooms = [r for r in visible if r.id in selected]
            area = sum(r.area_m2 or 0 for r in rooms)
            n = len(rooms)
            label = (
                "Select rooms to clean"
                if not n
                else f"Clean {n} room{'s' if n != 1 else ''}"
                + (f" · {area:.1f} m²" if area else "")
            )
            self.clean_button.set_label(label)
            self.clean_button.set_sensitive(
                bool(n) and usable and snap.state not in ("cleaning", "returning")
            )
            start = self.action_buttons["start"]
            cleaning = snap.state == "cleaning"
            start.set_icon_name(
                "media-playback-pause-symbolic" if cleaning else "media-playback-start-symbolic"
            )
            tip = "Pause" if cleaning else "Start"
            start.set_tooltip_text(tip)
            start.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            for button in self.action_buttons.values():
                button.set_sensitive(usable)
        finally:
            self._updating = False

    def _update_rooms(self, rooms, selected) -> None:
        key = [(r.id, r.name, r.area_m2, r.color) for r in rooms]
        if key != self._rooms_key:
            self._rooms_key = key
            self.rooms_list.remove_all()
            self.checks = {}
            for room in rooms:
                row = Adw.ActionRow(title=GLib.markup_escape_text(room.name))
                dot = Gtk.Box(
                    width_request=12,
                    height_request=12,
                    valign=Gtk.Align.CENTER,
                    css_classes=["room-dot", room_dot_class(room.color)],
                )
                row.add_prefix(dot)
                if room.area_m2 is not None:
                    row.add_suffix(
                        Gtk.Label(label=f"{room.area_m2:.1f} m²", css_classes=["dim-label"])
                    )
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                check.update_property([Gtk.AccessibleProperty.LABEL], [room.name])
                check.connect("toggled", self._on_check, room.id)
                row.add_suffix(check)
                row.set_activatable_widget(check)
                self.checks[room.id] = check
                self.rooms_list.append(row)
        for seg, check in self.checks.items():
            if check.get_active() != (seg in selected):
                check.set_active(seg in selected)

    def _on_check(self, check, seg) -> None:
        if not self._updating:
            self.emit("toggle-room", seg, check.get_active())
