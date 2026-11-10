import pytest
from gi.repository import Gio, GLib

from chippy.controller import VacuumController
from chippy.core.fake import FakeVacuum, build_fake_payload
from chippy.core.profiles import R2209
from chippy.dbus_service import INTERFACE, OBJECT_PATH, VacuumDBus
from chippy.store import MapCache, RoomStore
from tests.test_controller import InlineRunner, ManualScheduler

NAME = "io.github.eboye.ChippyTest"


@pytest.fixture
def bus():
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    server = Gio.DBusConnection.new_for_address_sync(test_bus.get_bus_address(), flags)
    client = Gio.DBusConnection.new_for_address_sync(test_bus.get_bus_address(), flags)
    server.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (NAME, 0)),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
    )
    yield server, client
    client.close_sync()
    server.close_sync()
    test_bus.down()


def _call(client, method, args=None):
    done = {}
    client.call(
        NAME,
        OBJECT_PATH,
        INTERFACE,
        method,
        args,
        None,
        Gio.DBusCallFlags.NONE,
        2000,
        None,
        lambda c, res: done.setdefault("r", res),
    )
    ctx = GLib.MainContext.default()
    while "r" not in done:
        ctx.iteration(True)
    return client.call_finish(done["r"])


def _get(client, prop):
    done = {}
    client.call(
        NAME,
        OBJECT_PATH,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (INTERFACE, prop)),
        None,
        Gio.DBusCallFlags.NONE,
        2000,
        None,
        lambda c, res: done.setdefault("r", res),
    )
    while "r" not in done:
        GLib.MainContext.default().iteration(True)
    return client.call_finish(done["r"]).unpack()[0]


def test_without_controller(bus):
    server, client = bus
    shown = []
    svc = VacuumDBus(lambda: shown.append(1))
    svc.register(server, OBJECT_PATH)
    assert _get(client, "State") == "offline"
    assert _get(client, "Rooms") == []
    _call(client, "ShowWindow")
    assert shown == [1]
    with pytest.raises(GLib.Error, match="Not signed in"):
        _call(client, "Start")
    svc.unregister()


def test_with_controller(bus, tmp_path):
    server, client = bus
    ctl = VacuumController(
        FakeVacuum(),
        R2209,
        RoomStore(tmp_path / "r.json", legacy=tmp_path / "x.json"),
        MapCache(tmp_path / "m"),
        build_fake_payload,
        runner=InlineRunner(),
        scheduler=ManualScheduler(),
    )
    svc = VacuumDBus(lambda: None)
    svc.register(server, OBJECT_PATH)
    changes = []
    client.signal_subscribe(
        NAME,
        "org.freedesktop.DBus.Properties",
        "PropertiesChanged",
        OBJECT_PATH,
        None,
        Gio.DBusSignalFlags.NONE,
        lambda *a: changes.append(a[-1].unpack()[1]),
    )
    svc.attach(ctl)
    ctl.start()
    assert _get(client, "State") == "docked"
    assert _get(client, "Battery") == 100
    assert [r[0] for r in _get(client, "Rooms")] == [1, 2, 3, 4]
    _call(client, "CleanRooms", GLib.Variant("(au)", ([2],)))
    assert ctl.snapshot.state == "cleaning"
    with pytest.raises(GLib.Error, match="unknown value"):
        _call(client, "SetSuction", GLib.Variant("(s)", ("nope",)))
    for _ in range(20):
        GLib.MainContext.default().iteration(False)
    assert any("State" in c for c in changes)
    assert all("Charging" not in c for c in changes[1:])
    changes.clear()
    svc._emit_changes()
    for _ in range(20):
        GLib.MainContext.default().iteration(False)
    assert changes == []
    ctl.stop()
    with pytest.raises(GLib.Error, match="stopped"):
        _call(client, "Start")
    svc.unregister()
