"""Main window: status pages, map + sidebar split view."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, GObject, Graphene, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402
from .mapview import MapView  # noqa: E402
from .sidebar import Sidebar  # noqa: E402

_BIDIRECTIONAL = GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE


CLOUD_EXPIRED = "Xiaomi sign-in expired — map updates are paused"


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
        map_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        map_body.append(self.cloud_banner)
        map_body.append(self.banner)
        map_body.append(overlay)
        map_view = Adw.ToolbarView(content=map_body)
        map_view.add_top_bar(map_header)
        self.map_header = map_header
        hint = Gtk.Label(
            label="Click rooms to select them · scroll to zoom · double-click to fit",
            css_classes=["caption", "dim-label"],
            margin_bottom=8,
        )
        map_view.add_bottom_bar(hint)

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
        scroller = Gtk.ScrolledWindow(
            child=self.sidebar, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        side_view = Adw.ToolbarView(content=scroller)
        side_view.add_top_bar(side_header)
        side_view.add_bottom_bar(self.sidebar.build_actions())

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
        self._update_subtitle()  # also on snapshots: cleaning → returning changes the wording
        self.map_view.set_selection(self.selected)
        self.sidebar.update(ctl, self.selected)
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

    # test hook -------------------------------------------------------------
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
            "checks": {str(k): centre(c) for k, c in self.sidebar.checks.items()},
            "buttons": {
                "clean": centre(self.sidebar.clean_button),
                "sign_in": centre(self.sign_in_button),
                "map": centre(self.map_view),
            },
            "live_robot": (
                list(self._ctl.live.robot)
                if self._ctl and self._ctl.live and self._ctl.live.robot
                else None
            ),
        }
