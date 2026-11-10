"""Preferences: background, top bar, rooms, account."""

import ipaddress
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import background  # noqa: E402
from ..shellext import (  # noqa: E402
    Installer,
    ShellExtensions,
    ShellExtError,
    bundled_source,
    extensions_root,
    live_shell_allowed,
)


def account_label(creds) -> str:
    """The account row's subtitle: the email/ID, or the user id for a QR sign-in."""
    if creds.username:
        return creds.username
    user_id = (creds.cloud_session or {}).get("user_id")
    return f"Xiaomi account {user_id}" if user_id else "Xiaomi account"


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "ChippyPreferences"

    def __init__(self, app):
        super().__init__(title="Preferences")
        self.app = app
        s = app.settings

        general = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        bg = Adw.PreferencesGroup(
            title="Background",
            description="Keep watching the robot and sending "
            "notifications when the window is closed.",
        )
        self.background_row = Adw.SwitchRow(title="Run in background", active=s.background)
        self.background_row.connect("notify::active", self._on_background)
        self.autostart_row = Adw.SwitchRow(
            title="Start at login", active=s.autostart, sensitive=s.background
        )
        self.autostart_row.connect("notify::active", self._on_autostart)
        bg.add(self.background_row)
        bg.add(self.autostart_row)
        general.add(bg)
        top = Adw.PreferencesGroup(title="Top bar")
        self.top_bar_row = Adw.SwitchRow(
            title="Show in top bar",
            subtitle="Adds a GNOME Shell extension with status and quick controls",
            active=s.top_bar,
        )
        if not live_shell_allowed(app.fake):
            # A fake run on the real session must never install into or enable on the real
            # Shell; only the nested check (its own private bus) keeps the switch live.
            self.top_bar_row.set_sensitive(False)
            self.top_bar_row.set_subtitle("Not available in fake mode")
        self.top_bar_row.connect("notify::active", self._on_top_bar)
        top.add(self.top_bar_row)
        general.add(top)
        self.add(general)

        self.rooms_page = Adw.PreferencesPage(title="Rooms", icon_name="view-grid-symbolic")
        self.rooms_group = Adw.PreferencesGroup(
            title="Rooms",
            description="Rename rooms or hide ones you never clean. Names stay on this computer.",
        )
        self.rooms_page.add(self.rooms_group)
        self.add(self.rooms_page)
        self._fill_rooms()

        account = Adw.PreferencesPage(title="Account", icon_name="avatar-default-symbolic")
        group = Adw.PreferencesGroup(title="Xiaomi account")
        creds, profile = app.credentials, app.profile
        if creds:
            group.add(
                Adw.ActionRow(
                    title="Signed in as",
                    subtitle=GLib.markup_escape_text(account_label(creds)),
                    subtitle_selectable=True,
                )
            )
            tested = "" if profile and profile.tested else " · not tested with Chippy"
            group.add(Adw.ActionRow(title="Robot", subtitle=f"{creds.model}{tested}"))
            self.ip_row = Adw.EntryRow(
                title="Robot IP address", text=creds.ip, show_apply_button=True
            )
            self.ip_row.connect("apply", self._on_ip)
            group.add(self.ip_row)
        sign_out = Gtk.Button(
            label="Sign Out",
            css_classes=["destructive-action", "pill"],
            halign=Gtk.Align.CENTER,
            margin_top=12,
        )
        sign_out.connect("clicked", self._on_sign_out)
        account.add(group)
        footer = Adw.PreferencesGroup()
        footer.add(sign_out)
        account.add(footer)
        self.add(account)

    def _fill_rooms(self) -> None:
        ctl = self.app.controller
        if ctl is None:
            self.rooms_group.set_description("Sign in to see your rooms.")
            return
        for room in ctl.rooms:
            area = f" · {room.area_m2:.1f} m²" if room.area_m2 is not None else ""
            row = Adw.EntryRow(
                title=f"Room {room.id}{area}", text=room.name, show_apply_button=True
            )
            row.connect("apply", lambda r, seg=room.id: self._rename(seg, r.get_text()))
            shown = Gtk.Switch(
                active=not room.hidden, valign=Gtk.Align.CENTER, tooltip_text="Show this room"
            )
            shown.connect(
                "notify::active",
                lambda sw, _p, seg=room.id: self._set_hidden(seg, not sw.get_active()),
            )
            row.add_suffix(shown)
            self.rooms_group.add(row)

    def _toast(self, text: str, timeout: int = 4) -> None:
        self.add_toast(Adw.Toast(title=text, timeout=timeout, use_markup=False))

    # The controller is looked up at change time: an IP change replaces it while this is open.
    def _rename(self, seg_id: int, name: str) -> None:
        if self.app.controller is None:
            self._toast("Not connected to the robot; sign in again")
            return
        self.app.controller.rename_room(seg_id, name)

    def _set_hidden(self, seg_id: int, hidden: bool) -> None:
        if self.app.controller is None:
            self._toast("Not connected to the robot; sign in again")
            return
        self.app.controller.set_room_hidden(seg_id, hidden)

    def _apply_autostart(self) -> None:
        s = self.app.settings
        try:
            background.set_autostart(s.background and s.autostart)
        except OSError as err:
            self._toast(f"Couldn't change start at login: {err}", 6)

    def _on_background(self, row, _p) -> None:
        s = self.app.settings
        if row.get_active() == s.background:
            return
        s.background = row.get_active()
        s.save()
        self.app.set_background_hold(s.background)
        self.autostart_row.set_sensitive(s.background)
        self._apply_autostart()

    def _on_autostart(self, row, _p) -> None:
        s = self.app.settings
        s.autostart = row.get_active()
        s.save()
        self._apply_autostart()

    def _on_top_bar(self, row, _p) -> None:
        want = row.get_active()
        if want == self.app.settings.top_bar or not live_shell_allowed(self.app.fake):
            return

        def work():
            installer = Installer(bundled_source(), extensions_root(), ShellExtensions())
            if want:
                return installer.install()
            installer.remove()
            return False

        def done(relogin, error):
            if error is not None:
                message = (
                    str(error)
                    if isinstance(error, ShellExtError)
                    else f"Couldn't change the top bar extension: {error}"
                )
                self._toast(message, 6)
                self.top_bar_row.set_active(self.app.settings.top_bar)
                return
            self.app.settings.top_bar = want
            self.app.settings.save()
            if relogin:
                self._toast("Log out and back in once to see Chippy in the top bar", 8)

        self.app.runner.run(work, done)

    def _on_ip(self, row) -> None:
        current = self.app.credentials
        text = row.get_text().strip()
        if current is None:
            return
        try:
            ipaddress.ip_address(text)
        except ValueError:
            self._toast("Enter an IP address like 192.168.1.20")
            return
        if text == current.ip:
            return

        def saved(error) -> None:
            if error is None:
                self._toast("Saved; reconnecting", 3)

        # A copy: app.credentials stays as it is until the new one is saved and running.
        self.app.sign_in_with(replace(current, ip=text), on_done=saved)

    def _on_sign_out(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading="Sign out?",
            body="Chippy will forget the robot's token and your Xiaomi session. Room names stay.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("sign-out", "Sign Out")
        dialog.set_response_appearance("sign-out", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_d, answer):
            if answer == "sign-out":
                self.close()
                self.app.sign_out()

        dialog.connect("response", response)
        dialog.present(self)
