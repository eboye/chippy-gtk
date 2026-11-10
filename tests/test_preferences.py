import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from chippy.core.credentials import Credentials  # noqa: E402
from chippy.mapmodel import Room  # noqa: E402
from chippy.store import Settings  # noqa: E402
from chippy.ui.preferences import PreferencesDialog  # noqa: E402


class StubController:
    def __init__(self):
        self.rooms = [Room(1, "Living room", 9.0, "#fff", None, False)]
        self.calls = []

    def rename_room(self, seg, name):
        self.calls.append(("rename", seg, name))

    def set_room_hidden(self, seg, hidden):
        self.calls.append(("hidden", seg, hidden))


class StubApp:
    def __init__(self, tmp_path, fake=True):
        self.fake = fake
        self.settings = Settings.load(tmp_path / "settings.json")
        self.controller = StubController()
        self.credentials = Credentials(
            ip="127.0.0.1",
            token="0" * 32,
            did="1",
            model="dreame.vacuum.r2209",
            country="de",
            username="u@example.com",
            password="pw",
        )
        self.profile = None
        self.signed_in = []

    def sign_in_with(self, creds, on_done=None):
        self.signed_in.append((creds, on_done))


def _room_widgets(prefs):
    found = {"entry": [], "switch": []}

    def walk(w):
        if isinstance(w, Adw.EntryRow) and w.get_title().startswith("Room "):
            found["entry"].append(w)
        if isinstance(w, Gtk.Switch):
            found["switch"].append(w)
        child = w.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(prefs.rooms_group)
    return found["entry"][0], found["switch"][0]


def test_top_bar_switch_is_off_limits_in_fake_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIPPY_FAKE_KEEP_APP_ID", raising=False)
    prefs = PreferencesDialog(StubApp(tmp_path))
    assert not prefs.top_bar_row.get_sensitive()
    assert prefs.top_bar_row.get_subtitle() == "Not available in fake mode"


def test_top_bar_switch_is_live_in_the_nested_check_and_real_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPPY_FAKE_KEEP_APP_ID", "1")
    assert PreferencesDialog(StubApp(tmp_path)).top_bar_row.get_sensitive()
    monkeypatch.delenv("CHIPPY_FAKE_KEEP_APP_ID")
    assert PreferencesDialog(StubApp(tmp_path, fake=False)).top_bar_row.get_sensitive()


def test_room_changes_go_to_the_current_controller(tmp_path):
    app = StubApp(tmp_path)
    prefs = PreferencesDialog(app)
    old = app.controller
    app.controller = StubController()  # e.g. an IP change restarted it
    entry, switch = _room_widgets(prefs)
    entry.set_text("Lounge")
    entry.emit("apply")
    switch.set_active(False)
    assert old.calls == []
    assert app.controller.calls == [("rename", 1, "Lounge"), ("hidden", 1, True)]
    app.controller = None  # signed out meanwhile: no crash, nothing sent
    entry.emit("apply")


def test_ip_change_is_validated_and_skipped_when_unchanged(tmp_path):
    app = StubApp(tmp_path)
    prefs = PreferencesDialog(app)
    for bad in ("", "not an ip", "300.1.1.1"):
        prefs.ip_row.set_text(bad)
        prefs.ip_row.emit("apply")
    prefs.ip_row.set_text("127.0.0.1")  # unchanged
    prefs.ip_row.emit("apply")
    assert app.signed_in == []
    prefs.ip_row.set_text(" 192.168.1.20 ")
    prefs.ip_row.emit("apply")
    [(creds, on_done)] = app.signed_in
    assert creds.ip == "192.168.1.20" and app.credentials.ip == "127.0.0.1"
    on_done(None)  # the toast only comes once the save is done
