"""Application: lifecycle, keyring, controller, D-Bus export, notifications."""

import functools
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import APP_ID, VERSION, background  # noqa: E402
from .controller import ThreadRunner, VacuumController  # noqa: E402
from .core import credentials  # noqa: E402
from .core.cloud import CloudMap, CloudSessionExpired, XiaomiCloud  # noqa: E402
from .core.device import Vacuum  # noqa: E402
from .core.fake import (  # noqa: E402
    FakeCloud,
    FakeMap,
    FakeVacuum,
    fake_live_payload,
    fake_saved_payload,
)
from .core.live import HISTORY_LIMIT, LIVE_PROPERTY, LiveTrack, latest_cleanset  # noqa: E402
from .core.profiles import UnsupportedModel, profile_for  # noqa: E402
from .dbus_service import VacuumDBus  # noqa: E402
from .store import MapCache, RoomStore, Settings  # noqa: E402
from .ui.sidebar import room_dot_css  # noqa: E402

log = logging.getLogger(__name__)
FAKE_CREDS = credentials.Credentials(
    ip="127.0.0.1",
    token="0" * 32,
    did="1001",
    model="dreame.vacuum.r2209",
    country="de",
    username="fake@example.com",
    password="fake",
)


class _MicloudFilter(logging.Filter):
    """micloud logs through the root logger, sometimes with 2FA URLs; keep WARNING+ only."""

    def filter(self, record: logging.LogRecord) -> bool:
        return f"{os.sep}micloud{os.sep}" not in record.pathname or record.levelno >= 30


class ChippyApp(Adw.Application):
    __gsignals__ = {"state-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS | Gio.ApplicationFlags.CAN_OVERRIDE_APP_ID,
        )
        self.add_main_option(
            "background",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Start without a window",
            None,
        )
        self.add_main_option(
            "fake",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Use a simulated robot (development)",
            None,
        )
        self.add_main_option(
            "test-state",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.FILENAME,
            "Write UI state to this file (testing)",
            "PATH",
        )
        self.add_main_option(
            "capture-live",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.FILENAME,
            "Record the robot's live map frames into this empty directory for 3 minutes",
            "DIR",
        )
        self.fake = False
        self._fake_root: str | None = None
        self._start_hidden = False
        self._held = False
        self._test_state_path: str | None = None
        self.state = "loading"
        self.controller: VacuumController | None = None
        self.credentials: credentials.Credentials | None = None
        self.profile = None
        self.settings: Settings | None = None
        self.runner = ThreadRunner()
        # Keyring load/save/delete run one at a time, in order (a delete can't race a save).
        self._keyring_runner = ThreadRunner(max_workers=1)
        # Bumped by every credentials change; a keyring callback from an older one is stale.
        self._generation = 0
        self._test_state_failing = False
        self._memory_keyring = None
        self._dbus = VacuumDBus(self.show_window)
        self.window = None

    # options and startup ---------------------------------------------------
    def do_handle_local_options(self, options) -> int:
        opts = options.end().unpack()
        self.fake = bool(opts.get("fake"))
        self._start_hidden = bool(opts.get("background"))
        self._test_state_path = _filename(opts.get("test-state"))
        capture_dir = _filename(opts.get("capture-live"))
        if capture_dir:
            return _capture_live(capture_dir, self.fake)
        if self.fake:
            # A fake must never reach (or be) the real instance on the session bus. Only the
            # nested-shell check, on its own private bus, keeps the real name for the extension.
            keep_id = os.environ.get("CHIPPY_FAKE_KEEP_APP_ID") == "1"
            if self.get_application_id() == APP_ID and not keep_id:
                self.set_application_id(APP_ID + ".Fake")
            # The fake must never touch the real config, cache or keyring.
            root = os.environ.get("CHIPPY_FAKE_HOME") or tempfile.mkdtemp(prefix="chippy-fake-")
            self._fake_root = root
            for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
                os.environ[var] = os.path.join(root, var.lower())
            self._memory_keyring = credentials.MemoryCollection()
            if os.environ.get("CHIPPY_FAKE_SIGNED_OUT") != "1":
                credentials.save(FAKE_CREDS, self._memory_keyring)
        return -1

    def do_dbus_register(self, connection, object_path) -> bool:
        Adw.Application.do_dbus_register(self, connection, object_path)
        self._dbus.register(connection, object_path)
        return True

    def do_dbus_unregister(self, connection, object_path) -> None:
        self._dbus.unregister()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(room_dot_css())
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.settings = Settings.load()
        self.set_background_hold(self.settings.background)
        self._add_actions()
        self._load_credentials()
        if self._test_state_path:
            GLib.timeout_add(300, self._write_test_state)
        if not self.fake:  # a fake must never write autostart files or ask the portal
            try:
                background.set_autostart(self.settings.background and self.settings.autostart)
            except OSError as err:  # an unwritable autostart dir must not stop the app
                log.warning("couldn't update autostart: %s", err)
        from .shellext import live_shell_allowed

        if self.settings.top_bar and live_shell_allowed(self.fake):
            # Keep the installed extension current when the switch is on.
            from .shellext import Installer, ShellExtensions, bundled_source, extensions_root

            def update():
                return Installer(
                    bundled_source(), extensions_root(), ShellExtensions()
                ).update_if_needed()

            def updated(_r, error):
                if error is not None:
                    log.warning("top bar extension update failed: %s", error)

            self.runner.run(update, updated)

    def do_activate(self) -> None:
        if self._start_hidden:
            self._start_hidden = False
            return
        self.show_window()

    def do_shutdown(self) -> None:
        if self.controller:
            self.controller.stop()
        self.runner.shutdown()
        self._keyring_runner.shutdown()
        Adw.Application.do_shutdown(self)

    def set_background_hold(self, on: bool) -> None:
        """Keep running with no window (background mode). A fake run never stays behind once
        its window closes, except in the nested check, which tests exactly that."""
        keep_fake = os.environ.get("CHIPPY_FAKE_KEEP_APP_ID") == "1"
        want = on and (not self.fake or keep_fake)
        if want != self._held:
            self._held = want
            (self.hold if want else self.release)()

    def show_window(self) -> None:
        from .ui.window import MainWindow

        if self.window is None:
            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_close)
        self.window.present()

    def _on_close(self, _window) -> bool:
        self.window = None
        return False  # destroy it; the app stays alive while held (background mode)

    # keyring and controller -----------------------------------------------
    def keyring(self):
        if self._memory_keyring is not None:
            return self._memory_keyring
        return credentials.default_collection()

    def _set_state(self, state: str) -> None:
        self.state = state
        self.emit("state-changed")

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _load_credentials(self) -> None:
        generation = self._next_generation()
        self._set_state("loading")
        import_legacy = not self.settings.signed_out

        def work():
            return credentials.load(self.keyring(), import_legacy=import_legacy)

        def done(creds, error):
            if generation != self._generation:
                return  # signed out or in meanwhile
            if isinstance(error, credentials.KeyringLocked):
                self._set_state("locked")
            elif error is not None:
                # Corrupt items raise JSONDecodeError/TypeError too; the message is safe to
                # log (it never contains the secret), the traceback is not needed.
                log.error("keyring failed: %s: %s", type(error).__name__, error)
                self._set_state("locked")
            elif creds is None:
                self._set_state("signed-out")
            else:
                self._start_controller(creds)

        self._keyring_runner.run(work, done)

    def retry_keyring(self) -> None:
        self._load_credentials()

    def _start_controller(self, creds) -> None:
        try:
            profile = profile_for(creds.model)
        except UnsupportedModel:
            log.warning("unsupported model %s", creds.model)
            self._set_state("signed-out")
            return
        if self.controller:
            self.controller.stop()
            self.controller = None
        if self.fake:
            fake_fault = os.environ.get("CHIPPY_FAKE_FAULT", "")  # testing: a reported fault
            fake_map = FakeMap()  # one map shared by the robot, the map fetch and live frames
            lag = os.environ.get("CHIPPY_FAKE_MAP_LAG", "")  # testing: the cloud's map lags
            fake_map.lag = int(lag) if lag.isdecimal() else 0
            # testing: the robot's room settings can't be read, with "Use room settings" on
            cleanset_unknown = os.environ.get("CHIPPY_FAKE_CLEANSET_UNKNOWN") == "1"
            fake_map.room_settings = cleanset_unknown
            vacuum = FakeVacuum(
                profile, int(fake_fault) if fake_fault.isdigit() else 0, fake_map=fake_map
            )
            fetch = functools.partial(fake_saved_payload, fake_map)
            if os.environ.get("CHIPPY_FAKE_CLOUD_EXPIRED") == "1":  # testing: a refused session

                def fetch():
                    raise CloudSessionExpired()

            def fetch_live():
                if not vacuum.request_live_map_object():
                    return None
                return fake_live_payload(vacuum.live_step, fake_map)

            def fetch_cleanset():
                return None if cleanset_unknown else fake_map.cleanset

            rooms = RoomStore(legacy=Path(self._fake_root or "/nonexistent") / "no-legacy.json")
        else:
            vacuum = Vacuum.connect(creds.ip, creds.token, profile)
            cloud_map = CloudMap(creds)

            def fetch():
                return cloud_map.fetch_payload(vacuum)

            def live_history(start, end):
                return cloud_map.property_history(
                    creds.did, LIVE_PROPERTY, start, end, HISTORY_LIMIT
                )

            live_track = LiveTrack(live_history)

            def fetch_live():
                if cloud_map.expired:
                    raise CloudSessionExpired()
                return live_track.poll()

            def fetch_cleanset():
                return latest_cleanset(live_history, int(time.time()))

            rooms = RoomStore()
        self.credentials, self.profile = creds, profile
        self.controller = VacuumController(
            vacuum,
            profile,
            rooms,
            MapCache(),
            fetch,
            fetch_live_payload=fetch_live,
            fetch_cleanset=fetch_cleanset,
        )
        self.controller.connect("notify-user", self._notify)
        self._dbus.attach(self.controller)
        self._set_state("ready")
        self.controller.start()

    def sign_in_with(self, creds, on_done=None) -> None:
        """Save `creds` and start on them; `on_done(error)` runs once that has happened
        (not if a later sign-in/out superseded this one)."""
        generation = self._next_generation()

        def work():
            credentials.save(creds, self.keyring())

        def done(_r, error):
            if generation != self._generation:
                return  # a later sign-in/out or reload superseded this one
            if error is not None:
                if self.window:
                    self.window.toast(f"Couldn't save to the keyring: {error}")
            else:
                self.settings.signed_out = False
                self.settings.save()
                self._start_controller(creds)
            if on_done is not None:
                on_done(error)

        self._keyring_runner.run(work, done)

    def sign_out(self) -> None:
        self._next_generation()
        if self.controller:
            self.controller.stop()
        self.controller = None
        self.credentials = None
        self._dbus.attach(None)
        self.settings.signed_out = True
        self.settings.save()
        MapCache().clear()

        def done(_r, error):
            if error is not None:
                log.error("couldn't remove the keyring item: %s", error)

        self._keyring_runner.run(lambda: credentials.delete(self.keyring()), done)
        self._set_state("signed-out")

    def cloud_factory(self, username: str, password: str):
        return (FakeCloud if self.fake else XiaomiCloud)(username, password)

    def _notify(self, _ctl, title: str, body: str) -> None:
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_default_action("app.show")
        self.send_notification(f"chippy-{title}", note)

    # actions and menu ------------------------------------------------------
    def primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Refresh Map", "app.refresh-map")
        menu.append("About Chippy", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    def _add_actions(self) -> None:
        def add(name, callback, accels=()):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", list(accels))

        add("show", self.show_window)
        add("quit", self.quit, ("<Control>q",))
        add("refresh-map", self._refresh_map, ("<Control>r",))
        add("about", self._about)
        add("preferences", self._preferences, ("<Control>comma",))
        add("sign-in", self._sign_in)
        add("sign-out", self.sign_out)

    def _refresh_map(self) -> None:
        if self.controller:
            self.controller.refresh_map()

    def _about(self) -> None:
        Adw.AboutDialog(
            application_name="Chippy",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="eboye",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/eboye/chippy-gtk",
        ).present(self.window)

    def _preferences(self) -> None:
        from .ui.preferences import PreferencesDialog

        self.show_window()
        PreferencesDialog(self).present(self.window)

    def _sign_in(self) -> None:
        from .ui.login import LoginDialog

        self.show_window()
        dialog = LoginDialog(self)
        dialog.connect("signed-in", lambda _d, creds: self.sign_in_with(creds))
        dialog.present(self.window)

    # test hook -------------------------------------------------------------
    def _write_test_state(self) -> bool:
        try:
            data = self.window.test_state() if self.window else {"page": None}
            data["app_state"] = self.state
            tmp = f"{self._test_state_path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._test_state_path)
        except Exception:  # noqa: BLE001 - a test hook must never stop the app
            if not self._test_state_failing:  # log once per failure streak, not every 300 ms
                log.exception("writing the test state failed")
            self._test_state_failing = True
        else:
            self._test_state_failing = False
        return GLib.SOURCE_CONTINUE


def _filename(value) -> str | None:
    if isinstance(value, bytes | bytearray | list):
        # FILENAME options arrive as a NUL-terminated bytestring ("ay": a list of ints).
        value = os.fsdecode(bytes(value).rstrip(b"\0"))
    return value or None


def _capture_live(directory: str, fake: bool) -> int:
    """`--capture-live DIR`: no window; record live frames from the real robot."""
    if fake:
        print("chippy: --capture-live needs the real robot; drop --fake", file=sys.stderr)
        return 2
    from .capture import run_capture

    try:
        creds = credentials.load(
            credentials.default_collection(), import_legacy=not Settings.load().signed_out
        )
    except Exception as err:  # noqa: BLE001 - a CLI tool reports and exits
        print(f"chippy: couldn't read the keyring: {type(err).__name__}", file=sys.stderr)
        return 1
    if creds is None:
        print("chippy: not signed in; sign in from the app first", file=sys.stderr)
        return 1
    try:
        profile = profile_for(creds.model)
    except UnsupportedModel:
        print(f"chippy: unsupported model {creds.model}", file=sys.stderr)
        return 1
    return run_capture(creds, profile, directory)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Logger filters don't see records propagated from child loggers; handler filters do.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_MicloudFilter())
    return ChippyApp().run(argv if argv is not None else sys.argv)
