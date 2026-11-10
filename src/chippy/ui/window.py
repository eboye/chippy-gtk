"""Main window: status pages, map + sidebar split view."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, GObject, Graphene, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402
from ..controller import MapNotUpdated  # noqa: E402
from ..core.mapedit import (  # noqa: E402
    EditSession,
    MapZones,
    are_neighbours,
    carry_edits,
    save_failure_text,
)
from ..mapmodel import geometry_from_frame, line_splits_room  # noqa: E402
from .editpanel import (  # noqa: E402
    NOT_SUPPORTED,
    ZONES_UNKNOWN,
    EditPanel,
    alert_buttons,
    confirm,
    mark_unsupported,
)
from .mapview import MapView  # noqa: E402
from .sidebar import Sidebar  # noqa: E402

_BIDIRECTIONAL = GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE


CLOUD_EXPIRED = "Xiaomi sign-in expired — map updates are paused"
BROWSE_HINT = "Click rooms to select them · scroll to zoom · double-click to fit"
# (tool, name, icon, hint) for the map toolbar in edit mode
EDIT_TOOLS = (
    (
        "select",
        "Select",
        "edit-select-symbolic",
        "Click a zone or wall to edit it · click a room for its settings",
    ),
    ("no_go", "No-go zone", "action-unavailable-symbolic", "Drag to draw a no-go zone"),
    ("no_mop", "No-mop zone", "weather-showers-symbolic", "Drag to draw a no-mop zone"),
    ("wall", "Wall", "list-remove-symbolic", "Drag to draw a virtual wall"),
    ("order", "Order", "view-list-ordered-symbolic", "Click rooms in the order to clean them"),
    ("split", "Split", "edit-cut-symbolic", "Click a room, then drag a line across it"),
    ("merge", "Merge", "video-joined-displays-symbolic", "Click two neighbouring rooms"),
)
_TOOL_KIND = {"no_go": "zones", "no_mop": "zones", "wall": "zones", "order": "order"}
STRUCTURAL_BODY = "This changes the robot's map right away."


def live_subtitle(state: str) -> str:
    return f"{'Returning' if state == 'returning' else 'Cleaning'} · robot position live"


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "ChippyMainWindow"

    def __init__(self, app):
        super().__init__(
            application=app,
            title="Chippy",
            default_width=1180,
            default_height=800,
            width_request=360,
            height_request=480,
        )
        self.app = app
        self.selected: set[int] = set()
        self._ctl = None  # the controller whose signals this window listens to
        self._ctl_handlers: list[int] = []
        # edit mode
        self.editing = False
        self._edit_generation = 0  # bumped on leaving: late callbacks of a left mode drop
        self._merge_first = 0  # first room picked with the Merge tool
        self._alert: Adw.AlertDialog | None = None  # merge / split confirmation
        self._toggling = False  # setting the Edit toggle from code
        self._could_edit = False
        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.toasts.set_child(self.stack)
        self.set_content(self.toasts)

        self.stack.add_named(
            self._status_page("Loading…", "Opening your keyring", None, None, spinner=True),
            "loading",
        )
        self.sign_in_button = Gtk.Button(label="Sign in", css_classes=["pill", "suggested-action"])
        self.sign_in_button.set_action_name("app.sign-in")
        self.stack.add_named(
            self._status_page(
                "Welcome to Chippy",
                "Sign in with your Xiaomi account to find your robot vacuum.",
                APP_ID,
                self.sign_in_button,
            ),
            "signed-out",
        )
        retry = Gtk.Button(label="Try again", css_classes=["pill"])
        retry.connect("clicked", lambda *_a: app.retry_keyring())
        self.stack.add_named(
            self._status_page(
                "Unlock your keyring",
                "Chippy keeps your robot's token in GNOME Keyring.",
                "dialog-password-symbolic",
                retry,
            ),
            "locked",
        )
        self.stack.add_named(self._main_page(), "main")

        self._app_handler = app.connect("state-changed", lambda *_a: self.sync_state())
        self.connect("close-request", lambda *_a: self.detach() or False)
        self.sync_state()

    def _status_page(self, title, desc, icon, child, spinner=False):
        page = Adw.StatusPage(title=title, description=desc, icon_name=icon)
        if spinner:
            page.set_paintable(Adw.SpinnerPaintable.new(page))
        if child is not None:
            child.set_halign(Gtk.Align.CENTER)
            page.set_child(child)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar(show_title=False))
        return view

    def _main_page(self):
        # map side
        self.map_view = MapView()
        self.map_view.connect(
            "room-clicked", lambda _v, seg: self.toggle_room(seg, seg not in self.selected)
        )
        self.map_title = Adw.WindowTitle(title="Map")
        map_header = Adw.HeaderBar(title_widget=self.map_title, show_end_title_buttons=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh map")
        refresh.set_action_name("app.refresh-map")
        map_header.pack_start(refresh)
        self.edit_button = Gtk.ToggleButton(
            icon_name="document-edit-symbolic", tooltip_text="Edit map", sensitive=False
        )
        self.edit_button.update_property([Gtk.AccessibleProperty.LABEL], ["Edit map"])
        self.edit_button.connect("toggled", self._on_edit_toggled)
        map_header.pack_start(self.edit_button)
        self.sidebar_toggle = Gtk.ToggleButton(
            icon_name="sidebar-show-right-symbolic", tooltip_text="Controls", visible=False
        )
        map_header.pack_end(self.sidebar_toggle)
        self.banner = Adw.Banner()
        self.cloud_banner = Adw.Banner(title=CLOUD_EXPIRED, button_label="Sign In")
        self.cloud_banner.set_action_name("app.sign-in")
        self.map_status = Adw.StatusPage(
            icon_name="image-missing-symbolic", title="No map yet", visible=False
        )
        overlay = Gtk.Overlay(child=self.map_view, vexpand=True)
        overlay.add_overlay(self.map_status)
        overlay.add_overlay(self._build_toolbar())
        self.map_view.connect("zones-edited", lambda _v, zones: self.edit_panel.set_zones(zones))
        self.map_view.connect(
            "zone-selected",
            lambda _v, kind, index: self.edit_panel.set_zone((kind, index) if index >= 0 else None),
        )
        self.map_view.connect("room-picked", lambda _v, seg: self._on_room_picked(seg))
        self.map_view.connect("split-line", lambda _v, seg, line: self._confirm_split(seg, line))
        map_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        map_body.append(self.cloud_banner)
        map_body.append(self.banner)
        map_body.append(overlay)
        map_view = Adw.ToolbarView(content=map_body)
        map_view.add_top_bar(map_header)
        self.map_header = map_header
        self.hint = Gtk.Label(
            label=BROWSE_HINT,
            css_classes=["caption", "dim-label"],
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        map_view.add_bottom_bar(self.hint)

        # sidebar side
        self.sidebar = Sidebar()
        self.sidebar.connect("toggle-room", lambda _s, seg, on: self.toggle_room(seg, on))
        self.sidebar.connect("select-all", lambda *_a: self.select_all())
        self.sidebar.connect("clean", lambda *_a: self.clean())
        self.sidebar.connect("level", lambda _s, kind, name: self._act(f"set_{kind}", name))
        self.sidebar.connect("action", lambda _s, name: self._act(name))
        side_header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Chippy"))
        menu = Gtk.MenuButton(
            icon_name="open-menu-symbolic", primary=True, menu_model=self.app.primary_menu()
        )
        menu.set_tooltip_text("Main menu")
        side_header.pack_end(menu)
        # edit mode swaps the controls (and their bottom bar) for the edit panel
        self.edit_panel = EditPanel()
        self.edit_panel.connect("changed", lambda *_a: self._edit_changed())
        self.edit_panel.connect("leave", lambda *_a: self._leave_edit())
        self.edit_panel.connect("save", lambda *_a: self._save_edit())
        self.side_stack = Gtk.Stack(vhomogeneous=False)
        self.side_stack.add_named(self.sidebar, "controls")
        self.side_stack.add_named(self.edit_panel, "edit")
        self.side_actions = Gtk.Stack(vhomogeneous=False)
        self.side_actions.add_named(self.sidebar.build_actions(), "controls")
        self.side_actions.add_named(self.edit_panel.build_actions(), "edit")
        scroller = Gtk.ScrolledWindow(
            child=self.side_stack, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        side_view = Adw.ToolbarView(content=scroller)
        side_view.add_top_bar(side_header)
        side_view.add_bottom_bar(self.side_actions)

        self.split = Adw.OverlaySplitView(
            content=map_view,
            sidebar=side_view,
            sidebar_position=Gtk.PackType.END,
            min_sidebar_width=340,
            max_sidebar_width=400,
        )
        self.split.bind_property("show-sidebar", self.sidebar_toggle, "active", _BIDIRECTIONAL)
        breakpoint_ = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint_.add_setter(self.split, "collapsed", True)
        breakpoint_.add_setter(self.sidebar_toggle, "visible", True)
        # Collapsed, the sidebar (and its window buttons) is hidden: show them on the map.
        breakpoint_.add_setter(self.map_header, "show-end-title-buttons", True)
        self.add_breakpoint(breakpoint_)
        return self.split

    # state -----------------------------------------------------------------
    def sync_state(self) -> None:
        state = self.app.state
        self.stack.set_visible_child_name(state if state != "ready" else "main")
        ctl = self.app.controller if state == "ready" else None
        if ctl is not self._ctl:
            self._disconnect_controller()
            if self.editing:
                self._leave_edit()  # signed out or restarted: the session belongs to the old one
            self.selected = set()
            if ctl is not None:
                self._ctl = ctl
                self._ctl_handlers = [
                    ctl.connect("snapshot-changed", lambda *_a: self.refresh()),
                    ctl.connect("rooms-changed", lambda *_a: self.refresh(map_too=True)),
                    ctl.connect("map-changed", lambda *_a: self.refresh(map_too=True)),
                    ctl.connect("action-failed", lambda _c, msg: self.toast(msg)),
                    ctl.connect("map-failed", lambda _c, msg: self.map_failed(msg)),
                    ctl.connect("live-changed", lambda *_a: self.live_changed()),
                    ctl.connect("cloud-expired", lambda *_a: self.refresh(map_too=True)),
                ]
                self.refresh(map_too=True)

    def _disconnect_controller(self) -> None:
        if self._ctl is not None:
            for handler in self._ctl_handlers:
                self._ctl.disconnect(handler)
        self._ctl, self._ctl_handlers = None, []

    def detach(self) -> None:
        """Stop listening to the app and controller (the window is going away)."""
        self._disconnect_controller()
        if self._app_handler:
            self.app.disconnect(self._app_handler)
            self._app_handler = 0

    def refresh(self, map_too: bool = False) -> None:
        ctl = self._ctl
        if ctl is None:
            return
        visible = {r.id for r in ctl.visible_rooms()}
        self.selected &= visible
        if map_too:
            self.map_view.set_map(ctl.grid, ctl.rooms)
            self.map_view.set_live(ctl.live)
            if ctl.grid is not None:
                self.map_status.set_visible(False)
            elif ctl.cloud_expired:
                self.map_status.set_title("Map unavailable")
                self.map_status.set_description("Sign in to Xiaomi again to load the map.")
                self.map_status.set_visible(True)
            elif not self.map_status.get_visible():
                self.map_status.set_title("No map yet")
                self.map_status.set_description(None)
                self.map_status.set_visible(True)
            self._show_zones()
        self._update_subtitle()  # also on snapshots: cleaning → returning changes the wording
        self.map_view.set_selection(self._map_selection())
        self.sidebar.update(ctl, self.selected)
        self._update_edit_allowed()
        self.cloud_banner.set_revealed(ctl.cloud_expired)
        snap = ctl.snapshot
        if snap.state == "offline" and ctl.failures:
            self.banner.set_title("Chippy can't reach the robot")
            self.banner.set_revealed(True)
        elif not ctl.profile.tested:
            self.banner.set_title(
                f"Chippy hasn't been tested with {ctl.profile.models[0]}; "
                "some controls may not work"
            )
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def live_changed(self) -> None:
        if self._ctl is not None:
            self.map_view.set_live(self._ctl.live)
            self._update_subtitle()

    def _update_subtitle(self) -> None:
        ctl = self._ctl
        if ctl.live is not None and ctl.live.robot is not None:
            self.map_title.set_subtitle(live_subtitle(ctl.snapshot.state))
            return
        visible = ctl.visible_rooms()
        total = sum(r.area_m2 or 0 for r in visible)
        self.map_title.set_subtitle(
            f"{total:.1f} m² mapped · {len(visible)} rooms" if ctl.grid else ""
        )

    def map_failed(self, message: str) -> None:
        self.toast(f"Map unavailable: {message}")
        if self._ctl is not None and self._ctl.grid is None:
            self.map_status.set_title("Map unavailable")
            self.map_status.set_description(message)
            self.map_status.set_visible(True)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4, use_markup=False))

    def _act(self, name: str, *args) -> None:
        if self._ctl is not None:
            self._ctl.act(name, *args)

    # selection -------------------------------------------------------------
    def toggle_room(self, seg: int, on: bool) -> None:
        (self.selected.add if on else self.selected.discard)(seg)
        self.refresh()

    def select_all(self) -> None:
        if self._ctl is None:
            return
        ids = {r.id for r in self._ctl.visible_rooms()}
        self.selected = set() if self.selected == ids else ids
        self.refresh()

    def clean(self) -> None:
        if self._ctl is None:
            return
        order = [r.id for r in self._ctl.visible_rooms() if r.id in self.selected]
        self._ctl.clean_rooms(order)

    # edit mode -------------------------------------------------------------
    def _build_toolbar(self) -> Gtk.Box:
        self.toolbar = Gtk.Box(
            css_classes=["linked"],
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
            margin_top=12,
            margin_start=12,
            visible=False,
        )
        self.tool_buttons: dict[str, Gtk.ToggleButton] = {}
        group = None
        for tool, name, icon, _hint in EDIT_TOOLS:
            button = Gtk.ToggleButton(icon_name=icon, tooltip_text=name, css_classes=["osd"])
            button.update_property([Gtk.AccessibleProperty.LABEL], [name])
            button.chippy_name = name  # its tooltip again once no longer "not supported"
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.connect("toggled", self._on_tool_toggled, tool)
            self.tool_buttons[tool] = button
            self.toolbar.append(button)
        return self.toolbar

    def _set_toggle(self, active: bool) -> None:
        self._toggling = True
        try:
            self.edit_button.set_active(active)
        finally:
            self._toggling = False

    def _on_edit_toggled(self, button) -> None:
        if self._toggling:
            return
        if button.get_active():
            self._enter_edit()
        elif self.editing:
            # stay in edit mode until the panel says so (it asks when there are changes)
            self._set_toggle(True)
            self.edit_panel.cancel()

    def _enter_edit(self) -> None:
        ctl = self._ctl
        if ctl is None or not ctl.can_edit:
            self._set_toggle(False)
            return
        self.editing = True
        self._set_toggle(True)
        self.side_stack.set_visible_child_name("edit")
        self.side_actions.set_visible_child_name("edit")
        self.toolbar.set_visible(True)
        for button in self.tool_buttons.values():
            mark_unsupported(button, False, default=button.chippy_name)
        self._choose_tool("select")
        self._load_session()

    def _load_session(self, carry: tuple[EditSession, EditSession] | None = None) -> None:
        """Ask the controller for a session. After a merge or split, `carry` is the previous
        (original, edited) pair whose unsaved edits move onto the new session."""
        ctl, generation = self._ctl, self._edit_generation
        self._set_busy(True)
        self.edit_panel.begin()

        def done(session, error) -> None:
            if generation != self._edit_generation or ctl is not self._ctl:
                return
            self._set_busy(False)
            if error is not None:
                self.toast(f"Can't edit the map: {error}")
                self._leave_edit()
                return
            edited = carry_edits(*carry, session) if carry is not None else session.copy()
            shown = {r.id for r in ctl.visible_rooms()}
            self.edit_panel.set_session(session, edited, ctl.profile, shown)
            self.edit_panel.set_tool(self.map_view.tool)
            self._apply_unsupported_tools()
            self._edit_changed()

        ctl.begin_edit(done)

    def _leave_edit(self) -> None:
        self._edit_generation += 1
        self.editing = False
        self._merge_first = 0
        self._close_alerts()
        self._set_busy(False)
        self.edit_panel.set_saving(False)
        self.edit_panel.begin()
        self._set_toggle(False)
        self.side_stack.set_visible_child_name("controls")
        self.side_actions.set_visible_child_name("controls")
        self.toolbar.set_visible(False)
        self.map_view.set_tool(None)
        self.map_view.set_zones_editable(True)
        self.map_view.set_sensitive(True)
        self.hint.set_label(BROWSE_HINT)
        self.refresh(map_too=True)

    def _close_alerts(self) -> None:
        for alert in (self._alert, self.edit_panel.alert):
            if alert is not None:
                alert.force_close()
        self._alert = self.edit_panel.alert = None

    def _set_busy(self, busy: bool) -> None:
        """Loading a session, saving, merging or splitting: no tools, no map input."""
        self.toolbar.set_sensitive(not busy)
        self.map_view.set_sensitive(not busy)

    def _choose_tool(self, tool: str) -> None:
        button = self.tool_buttons[tool]
        if button.get_active():
            self._use_tool(tool)
        else:
            button.set_active(True)  # its "toggled" handler calls _use_tool

    def _on_tool_toggled(self, button, tool) -> None:
        if button.get_active() and self.editing:
            self._use_tool(tool)

    def _use_tool(self, tool: str) -> None:
        self._merge_first = 0
        self.map_view.set_tool(tool)
        self.edit_panel.set_tool(tool)
        self.edit_panel.set_zone(self.map_view.selected_zone)
        self._show_tool_hint()
        self.map_view.set_selection(self._map_selection())

    def _apply_unsupported_tools(self) -> None:
        edited = self.edit_panel.edited
        off = set(edited.unsupported) if edited is not None else set()
        why = {kind: NOT_SUPPORTED for kind in off}
        if edited is not None and not edited.zones_known and "zones" not in off:
            off.add("zones")  # unreadable zones are read-only, like refused ones
            why["zones"] = ZONES_UNKNOWN
        for tool, kind in _TOOL_KIND.items():
            button = self.tool_buttons[tool]
            mark_unsupported(
                button, kind in off, why.get(kind, NOT_SUPPORTED), default=button.chippy_name
            )
        self.map_view.set_zones_editable("zones" not in off)
        if _TOOL_KIND.get(self.map_view.tool) in off:
            self._choose_tool("select")

    def _edit_changed(self) -> None:
        """The staged session changed: redraw its zones and order on the map."""
        self._show_zones()
        self.map_view.set_selection(self._map_selection())

    def _show_zones(self) -> None:
        ctl = self._ctl
        frame = ctl.current_frame if ctl is not None else None
        geom = geometry_from_frame(frame) if frame is not None else None
        edited = self.edit_panel.edited if self.editing else None
        if edited is not None:
            self.map_view.set_zones(edited.zones, geom)
            self.map_view.set_order(edited.order)
            self.edit_panel.set_zone(self.map_view.selected_zone)  # set_zones may clear it
        else:
            self.map_view.set_zones(frame.zones if frame is not None else MapZones(), geom)
            self.map_view.set_order(None)

    def _map_selection(self) -> set[int]:
        """Rooms highlighted on the map: the room selection, or in edit mode the picked room."""
        if not self.editing:
            return self.selected
        if self.map_view.tool == "merge":
            return {self._merge_first} if self._merge_first else set()
        return {self.edit_panel.room} if self.edit_panel.room else set()

    def _update_edit_allowed(self) -> None:
        """The Edit toggle follows `can_edit`; edit mode ends when the robot gets busy."""
        can = self._ctl is not None and self._ctl.can_edit
        saving = self.edit_panel.saving
        self.edit_button.set_sensitive(can and not saving)
        self.edit_panel.set_allowed(can)
        if saving:
            return  # judged again when the save is over (`_could_edit` keeps its value)
        if self.editing and self._could_edit and not can:
            if self._alert is not None:  # never two dialogs: the merge/split question goes
                alert, self._alert = self._alert, None
                alert.force_close()
                self._alert_closed()
            self.edit_panel.cancel()  # leaves at once, or asks first when there are changes
        self._could_edit = can

    def _room_name(self, seg: int) -> str:
        edited = self.edit_panel.edited
        name = edited.names.get(seg, "").strip() if edited is not None else ""
        if not name and self._ctl is not None:
            name = next((r.name for r in self._ctl.rooms if r.id == seg), "")
        return name or f"Room {seg}"

    def _on_room_picked(self, seg: int) -> None:
        if not self.editing or self.edit_panel.edited is None:
            return
        if self.map_view.tool == "merge":
            if not self._merge_first or self._merge_first == seg:
                self._merge_first = 0 if self._merge_first == seg else seg
                self._show_tool_hint()
            elif not self._neighbours(self._merge_first, seg):
                self.hint.set_label(f"Pick a room next to {self._room_name(self._merge_first)}")
            else:
                self._confirm_merge(self._merge_first, seg)
        else:
            self.edit_panel.pick_room(seg)
        self.map_view.set_selection(self._map_selection())

    def _neighbours(self, a: int, b: int) -> bool:
        frame = self._ctl.current_frame if self._ctl is not None else None
        return frame is None or are_neighbours(frame.neighbours, a, b)

    def _confirm_merge(self, a: int, b: int) -> None:
        if self._alert is not None:
            return
        self._alert = confirm(
            self,
            f"Merge {self._room_name(a)} and {self._room_name(b)}?",
            STRUCTURAL_BODY,
            ("merge", "Merge"),
            lambda: self._structural("merge", a, b),
            on_close=self._alert_closed,
        )

    def _confirm_split(self, seg: int, line) -> None:
        if not self.editing or self.edit_panel.edited is None or self._alert is not None:
            return
        ctl = self._ctl
        frame = ctl.current_frame if ctl is not None else None
        if (
            frame is None
            or ctl.grid is None
            or not line_splits_room(ctl.grid, geometry_from_frame(frame), seg, line)
        ):
            self.hint.set_label(f"Draw the line across {self._room_name(seg)}, edge to edge")
            return
        self._alert = confirm(
            self,
            f"Split {self._room_name(seg)}?",
            STRUCTURAL_BODY,
            ("split", "Split"),
            lambda: self._structural("split", seg, line),
            on_close=self._alert_closed,
        )

    def _show_tool_hint(self) -> None:
        tool = self.map_view.tool
        self.hint.set_label(next((h for t, _n, _i, h in EDIT_TOOLS if t == tool), BROWSE_HINT))

    def _alert_closed(self) -> None:
        self._alert = None
        self._merge_first = 0
        if self.editing:
            self._show_tool_hint()  # a "Pick a room next to ..." hint is answered
        self.map_view.set_selection(self._map_selection())

    def _structural(self, kind: str, *args) -> None:
        """Merge or split on the robot now, then reload the session from the new map."""
        ctl, generation, panel = self._ctl, self._edit_generation, self.edit_panel
        if ctl is None or panel.edited is None:
            return
        original, edited, panel_shown = panel.original, panel.edited, panel.shown
        self._set_busy(True)
        panel.begin()

        def done(error) -> None:
            if generation != self._edit_generation or ctl is not self._ctl:
                return
            if isinstance(error, MapNotUpdated):
                # the robot did it: never go back to the session from before it
                self.toast(str(error))
                self._leave_edit()
                return
            if error is not None:
                self._set_busy(False)
                self.toast(f"{kind.capitalize()} failed: {error}")
                panel.set_session(original, edited, ctl.profile, panel_shown)
                panel.set_tool(self.map_view.tool)
                self._edit_changed()
                return
            self._load_session(carry=(original, edited))

        if kind == "merge":
            ctl.merge_rooms(edited.map_id, *args, done)
        else:
            ctl.split_room(edited.map_id, *args, done)

    def _save_edit(self) -> None:
        ctl, generation, panel = self._ctl, self._edit_generation, self.edit_panel
        if ctl is None or panel.edited is None or not panel.dirty:
            return
        panel.set_saving(True)
        self._set_busy(True)
        self.edit_button.set_sensitive(False)

        def done(error, failed, done_kinds) -> None:
            if generation != self._edit_generation or ctl is not self._ctl:
                return
            panel.set_saving(False)
            self._set_busy(False)
            if error is None or isinstance(error, MapNotUpdated):
                self.toast("Saved to robot" if error is None else str(error))
                self._leave_edit()
                return
            self.toast(save_failure_text(error, failed, done_kinds))
            panel.mark_saved(done_kinds)  # not resent, not a change any more
            panel.apply_unsupported()  # a kind the model lacks is disabled; edits are kept
            self._apply_unsupported_tools()
            self._edit_changed()
            self._update_edit_allowed()

        ctl.save_edit(panel.original, panel.edited, done)

    # test hook -------------------------------------------------------------
    def _edit_test_state(self, centre, dialog) -> dict:
        zones = self.map_view.zones
        buttons = {tool: centre(button) for tool, button in self.tool_buttons.items()}
        buttons["save"] = centre(self.edit_panel.save_button)
        buttons["cancel"] = centre(self.edit_panel.cancel_button)
        buttons["edit"] = centre(self.edit_button)
        buttons["name"] = centre(self.edit_panel.name_row)
        return {
            "mode": self.editing,
            "loading": self.editing and self.edit_panel.edited is None,
            "room_names": {str(r.id): r.name for r in self.map_view.rooms},
            "tool": self.map_view.tool,
            "dirty": self.edit_panel.dirty,
            "room": self.edit_panel.room,
            "hint": self.hint.get_label(),
            "save_sensitive": self.edit_panel.save_button.get_sensitive(),
            "buttons": buttons,
            "zones": {
                "no_go": len(zones.no_go),
                "no_mop": len(zones.no_mop),
                "walls": len(zones.walls),
            },
            "no_go_rects": [r.as_list() for r in zones.no_go],
            "selected_zone": list(self.map_view.selected_zone or ()) or None,
            # an open confirmation (Discard / Merge / Split) and its buttons by response id;
            # Return accepts Merge / Split but keeps editing on Discard
            "alert": (
                {
                    "heading": dialog.get_heading(),
                    "buttons": {r: centre(b) for r, b in alert_buttons(dialog).items()},
                }
                if isinstance(dialog, Adw.AlertDialog)
                else None
            ),
        }

    def test_state(self) -> dict:
        native = self.get_native()
        sx, sy = native.get_surface_transform() if native else (0.0, 0.0)

        def point(widget, x, y):
            ok, p = widget.compute_point(self, Graphene.Point().init(x, y))
            return [p.x + sx, p.y + sy] if ok else None

        def centre(widget):
            if not widget.get_mapped():
                return None
            return point(widget, widget.get_width() / 2, widget.get_height() / 2)

        vp = self.map_view.viewport
        dialog = self.get_visible_dialog()
        width, height = self.map_view.get_width(), self.map_view.get_height()
        rooms = {}
        if self.map_view.get_mapped():
            rooms = {
                str(k): point(self.map_view, *v)
                for k, v in self.map_view.label_points().items()
                if 0 <= v[0] <= width and 0 <= v[1] <= height
            }
        return {
            "page": self.stack.get_visible_child_name(),
            "selected": sorted(self.selected),
            "clean_label": self.sidebar.clean_button.get_label(),
            "clean_sensitive": self.sidebar.clean_button.get_sensitive(),
            "scale": vp.scale if vp else 0,
            "fit_scale": vp.fit_scale() if vp else 0,
            "sidebar_collapsed": self.split.get_collapsed(),
            "cloud_banner": (
                [self.cloud_banner.get_title(), self.cloud_banner.get_action_name()]
                if self.cloud_banner.get_revealed()
                else None
            ),
            "dialog": getattr(dialog, "test_page", lambda: None)() if dialog else None,
            "dialog_buttons": {
                name: [p[0] + sx, p[1] + sy] if p else None
                for name, p in (getattr(dialog, "test_buttons", dict)() if dialog else {}).items()
            },
            "rooms": rooms,
            "room_names": {str(r.id): r.name for r in self.map_view.rooms if not r.hidden},
            "checks": {str(k): centre(c) for k, c in self.sidebar.checks.items()},
            "buttons": {
                "clean": centre(self.sidebar.clean_button),
                "sign_in": centre(self.sign_in_button),
                "map": centre(self.map_view),
            },
            "edit": self._edit_test_state(centre, dialog),
            "live_robot": (
                list(self._ctl.live.robot)
                if self._ctl and self._ctl.live and self._ctl.live.robot
                else None
            ),
        }
