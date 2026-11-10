"""Sign in to Xiaomi: password, captcha, email code, pick the vacuum."""

from collections.abc import Callable

import gi
import requests
from micloud.micloudexception import MiCloudAccessDenied

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from ..core.accounts import credentials_for, discover_vacuums  # noqa: E402
from ..core.cloud import CaptchaRequired, CloudUnreachable, TwoFactorRequired  # noqa: E402

ATTEMPTS = 3


NETWORK_ERROR = "Couldn't reach Xiaomi. Check your connection."


def _message(error: BaseException) -> str:
    if isinstance(error, CloudUnreachable | requests.RequestException):
        return NETWORK_ERROR  # never the request's own text: its URL can carry secrets
    return str(error)


class LoginFlow:
    """GTK-free sign-in state machine; `on_page(name, data)` drives the UI.

    One request at a time: while `busy`, further submissions are ignored. After ATTEMPTS
    wrong email codes, `code_locked` is set and only `resend_code()` unlocks it.
    """

    def __init__(self, cloud_factory, runner, on_page: Callable[[str, dict], None]):
        self._factory, self._runner, self._page_cb = cloud_factory, runner, on_page
        self._cloud = None
        self._username = self._password = ""
        self._captcha_tries = self._code_tries = 0
        self._two_factor_url = ""
        self.busy = False
        self.code_locked = False

    def _on_page(self, name: str, data: dict) -> None:
        self.busy = False
        self._page_cb(name, data)

    def _start(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        return True

    def _error(self, page: str, message: str) -> None:
        self._on_page("error", {"page": page, "message": message})

    def sign_in(self, username: str, password: str) -> None:
        if not self._start():
            return
        self._username, self._password = username.strip(), password
        self._cloud = self._factory(self._username, password)
        self._captcha_tries = self._code_tries = 0
        self.code_locked = False
        self._login()

    def _login(self) -> None:
        self._runner.run(self._cloud.login, self._after_login)

    def _after_login(self, ok, error) -> None:
        if isinstance(error, CaptchaRequired):
            url = error.url
            self._runner.run(lambda: self._cloud.fetch_captcha(url), self._show_captcha)
        elif isinstance(error, TwoFactorRequired):
            self._two_factor_url = error.url
            self._runner.run(
                lambda: self._cloud.send_email_code(self._two_factor_url),
                lambda _r, err: (
                    self._error("account", _message(err)) if err else self._on_page("code", {})
                ),
            )
        elif error is not None:
            self._error("account", _message(error))
        elif not ok:
            self._error("account", "Xiaomi rejected the email or password.")
        else:
            self._discover()

    def _show_captcha(self, image, error) -> None:
        if error is not None:
            self._error("account", f"Couldn't load the captcha: {_message(error)}")
        else:
            self._on_page("captcha", {"image": image})

    def submit_captcha(self, code: str) -> None:
        if self._cloud is None or not self._start():
            return
        self._captcha_tries += 1
        self._cloud.captcha_code = code.strip()
        self._login_after_captcha()

    def _login_after_captcha(self) -> None:
        def after(ok, error):
            if isinstance(error, CaptchaRequired) and self._captcha_tries >= ATTEMPTS:
                self._error("captcha", "Xiaomi kept asking for a captcha; try again later.")
            else:
                self._after_login(ok, error)

        self._runner.run(self._cloud.login, after)

    def submit_code(self, code: str) -> None:
        if self._cloud is None or self.code_locked or not self._start():
            return
        self._code_tries += 1

        def after(_r, error):
            if isinstance(error, MiCloudAccessDenied):
                if self._code_tries >= ATTEMPTS:
                    self.code_locked = True
                    self._error("code", f"{error}. Send a new code to try again.")
                else:
                    self._error("code", str(error))
            elif error is not None:
                self._error("code", _message(error))
            else:
                self._discover()

        self._runner.run(lambda: self._cloud.verify_email_code(code.strip()), after)

    def resend_code(self) -> None:
        if self._cloud is None or not self._start():
            return

        def after(_r, error):
            if error is not None:
                self._error("code", _message(error))
            else:
                self._code_tries = 0
                self.code_locked = False
                self._on_page("code", {"resent": True})

        self._runner.run(lambda: self._cloud.send_email_code(self._two_factor_url), after)

    def _discover(self) -> None:
        def after(found, error):
            if error is not None:
                self._error("account", _message(error))
            elif not any(v.supported for v in found):
                self._error("account", "No Dreame vacuum on this Xiaomi account.")
            else:
                self._on_page("devices", {"vacuums": found})

        self._runner.run(lambda: discover_vacuums(self._cloud), after)

    def choose(self, vacuum) -> None:
        creds = credentials_for(vacuum, self._username, self._password, self._cloud.session_state())
        self._on_page("done", {"credentials": creds})


class LoginDialog(Adw.Dialog):
    __gtype_name__ = "ChippyLoginDialog"
    __gsignals__ = {"signed-in": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, app):
        super().__init__(title="Sign in to Xiaomi", content_width=440, content_height=520)
        self._flow = LoginFlow(app.cloud_factory, app.runner, self._on_page)
        self._closed = False
        self.connect("closed", self._on_closed)
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)

        self.email = Adw.EntryRow(title="Email or Xiaomi ID")
        self.password = Adw.PasswordEntryRow(title="Password")
        self.password.connect("entry-activated", lambda *_a: self._submit_account())
        self.email.connect("entry-activated", lambda *_a: self.password.grab_focus())
        self.account_error = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        self.account_button = Gtk.Button(label="Sign In", css_classes=["pill", "suggested-action"])
        self.account_button.connect("clicked", lambda *_a: self._submit_account())
        hint = Gtk.Label(
            label="Signed in to Mi Home with Google? Set a Xiaomi password in "
            "your account settings first.",
            wrap=True,
            css_classes=["caption", "dim-label"],
        )
        self.nav.add(
            self._page(
                "account",
                "Sign In",
                [
                    self._group(self.email, self.password),
                    hint,
                    self.account_error,
                    self.account_button,
                ],
            )
        )
        # The email row has the keyboard focus as soon as the dialog shows.
        self.connect("map", lambda *_a: GLib.idle_add(self._focus, self.email))

        self.captcha_picture = Gtk.Picture(height_request=60, can_shrink=False)
        self.captcha = Adw.EntryRow(title="Characters in the image")
        self.captcha.connect("entry-activated", lambda *_a: self._submit_captcha())
        self.captcha_error = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        self.captcha_button = Gtk.Button(label="Continue", css_classes=["pill", "suggested-action"])
        self.captcha_button.connect("clicked", lambda *_a: self._submit_captcha())
        self.captcha_page = self._page(
            "captcha",
            "Captcha",
            [
                Gtk.Label(label="Xiaomi wants to check you're a person.", wrap=True),
                self.captcha_picture,
                self._group(self.captcha),
                self.captcha_error,
                self.captcha_button,
            ],
        )

        self.code = Adw.EntryRow(title="Code from the email", input_purpose=Gtk.InputPurpose.DIGITS)
        self.code.connect("entry-activated", lambda *_a: self._submit_code())
        self.code_error = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        self.verify_button = Gtk.Button(label="Verify", css_classes=["pill", "suggested-action"])
        self.verify_button.connect("clicked", lambda *_a: self._submit_code())
        self.resend_button = Gtk.Button(label="Send a new code", css_classes=["flat"])
        self.resend_button.connect("clicked", lambda *_a: self._resend())
        self.code_page = self._page(
            "code",
            "Check Your Email",
            [
                Gtk.Label(
                    label="Xiaomi sent a code to your account's email. It can take a few "
                    "minutes to arrive.",
                    wrap=True,
                ),
                self._group(self.code),
                self.code_error,
                self.verify_button,
                self.resend_button,
            ],
        )

        self.devices = Gtk.ListBox(css_classes=["boxed-list"])
        self.devices.connect("row-activated", self._on_device)
        self.devices_page = self._page("devices", "Choose Your Vacuum", [self.devices])

    def _group(self, *rows):
        group = Adw.PreferencesGroup()
        for row in rows:
            group.add(row)
        return group

    def _page(self, tag, title, children):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        for child in children:
            box.append(child)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=box, propagate_natural_height=True))
        view.add_top_bar(Adw.HeaderBar())
        return Adw.NavigationPage(title=title, tag=tag, child=view)

    @staticmethod
    def _focus(widget) -> bool:
        widget.grab_focus()
        return GLib.SOURCE_REMOVE

    def _on_closed(self, _dialog) -> None:
        self._closed = True

    def test_page(self) -> str:
        page = self.nav.get_visible_page()
        return (page.get_tag() if page else None) or "account"

    def _busy(self, busy: bool) -> None:
        """Lock every input while a request runs (the flow also ignores extra submissions)."""
        self.account_button.set_sensitive(not busy)
        self.account_button.set_label("Signing In…" if busy else "Sign In")
        focus = self.get_focus()
        if (
            busy
            and focus is not None
            and any(focus == w or focus.is_ancestor(w) for w in (self.captcha, self.code))
        ):
            # Unfocus first: GTK warns when a focused text goes insensitive. The page that
            # answers puts the focus back.
            self.set_focus(None)
        for widget in (self.captcha, self.captcha_button, self.resend_button):
            widget.set_sensitive(not busy)
        for widget in (self.code, self.verify_button):
            widget.set_sensitive(not busy and not self._flow.code_locked)

    def _submit_account(self) -> None:
        if self._flow.busy or not self.email.get_text().strip() or not self.password.get_text():
            return
        self.account_error.set_visible(False)
        self._busy(True)
        self._flow.sign_in(self.email.get_text(), self.password.get_text())

    def _submit_captcha(self) -> None:
        if self._flow.busy:
            return
        self.captcha_error.set_visible(False)
        self._busy(True)
        self._flow.submit_captcha(self.captcha.get_text())

    def _submit_code(self) -> None:
        if self._flow.busy or self._flow.code_locked:
            return
        self.code_error.set_visible(False)
        self._busy(True)
        self._flow.submit_code(self.code.get_text())

    def _resend(self) -> None:
        if self._flow.busy:
            return
        self.code_error.set_visible(False)
        self._busy(True)
        self._flow.resend_code()

    def _push(self, page, tag) -> None:
        if self.nav.get_visible_page() is not page:
            if self.nav.find_page(tag) is None:
                self.nav.push(page)
            else:
                self.nav.pop_to_tag(tag)

    def _on_page(self, name: str, data: dict) -> None:
        if self._closed:
            return  # cancelled: a late answer must not sign in or touch the dialog
        self._busy(False)
        if name == "captcha":
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data["image"]))
            except GLib.Error:
                self._on_page(
                    "error",
                    {"page": "account", "message": "Couldn't show the captcha image; try again."},
                )
                return
            self.captcha_picture.set_paintable(texture)
            self.captcha.set_text("")
            self._push(self.captcha_page, "captcha")
            GLib.idle_add(self._focus, self.captcha)
        elif name == "code":
            if data.get("resent"):
                self.code.set_text("")
            self._push(self.code_page, "code")
            GLib.idle_add(self._focus, self.code)
        elif name == "devices":
            self.devices.remove_all()
            first = None
            for v in data["vacuums"]:
                suffix = (
                    "" if v.tested else (" · not tested" if v.supported else " · not supported")
                )
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(v.name),
                    subtitle=GLib.markup_escape_text(v.model + suffix),
                    activatable=v.supported,
                    sensitive=v.supported,
                )
                row.vacuum = v
                self.devices.append(row)
                if first is None and v.supported:
                    first = row
            self._push(self.devices_page, "devices")
            if sum(1 for v in data["vacuums"] if v.supported) == 1 and first is not None:
                self._flow.choose(first.vacuum)
            elif first is not None:
                GLib.idle_add(self._focus, first)
        elif name == "done":
            self.emit("signed-in", data["credentials"])
            self.close()
        elif name == "error":
            label = {
                "account": self.account_error,
                "captcha": self.captcha_error,
                "code": self.code_error,
            }.get(data["page"], self.account_error)
            label.set_label(data["message"])
            label.set_visible(True)
            if data["page"] == "account":
                self._push(self.nav.find_page("account"), "account")
            if data["page"] == "captcha" and "captcha" not in data["message"].lower():
                GLib.idle_add(self._focus, self.captcha)
            elif data["page"] == "code":
                # Locked after too many wrong codes: only "Send a new code" is left.
                target = self.resend_button if self._flow.code_locked else self.code
                GLib.idle_add(self._focus, target)

    def _on_device(self, _list, row) -> None:
        self._flow.choose(row.vacuum)
