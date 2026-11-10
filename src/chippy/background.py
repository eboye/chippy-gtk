"""Run in the background and start at login."""

import logging
import os
import shutil
import sys
from pathlib import Path

from gi.repository import Gio, GLib

from . import APP_ID
from .shellext import in_flatpak

log = logging.getLogger(__name__)
AUTOSTART_REASON = "Watch the robot and send notifications"


def autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / f"{APP_ID}.desktop"


def _command() -> str:
    return shutil.which("chippy") or f"{sys.executable} -m chippy"


def set_autostart(enabled: bool, reason: str = AUTOSTART_REASON) -> None:
    if in_flatpak():
        _portal(enabled, reason)
        return
    path = autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=Chippy\n"
        f"Exec={_command()} --background\nIcon={APP_ID}\n"
        "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
    )


def _portal(enabled: bool, reason: str) -> None:
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Background",
            None,
        )
        options = {
            "reason": GLib.Variant("s", reason),
            "autostart": GLib.Variant("b", enabled),
            "commandline": GLib.Variant("as", ["chippy", "--background"]),
            "dbus-activatable": GLib.Variant("b", False),
        }
        proxy.call(
            "RequestBackground",
            GLib.Variant("(sa{sv})", ("", options)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
    except GLib.Error as err:
        log.warning("background portal unavailable: %s", err.message)
