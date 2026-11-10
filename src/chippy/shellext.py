"""Install/update/remove the bundled GNOME Shell extension (a copy, never a symlink)."""

import json
import os
import shutil
from pathlib import Path

from gi.repository import Gio, GLib

UUID = "chippy@eboye.github.io"
MARKER = ".chippy-installed"


class ShellExtError(Exception):
    pass


def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def live_shell_allowed(fake: bool, environ=None) -> bool:
    """May this run install into / enable on the Shell it talks to? A fake run only may inside
    the nested check, which sets CHIPPY_FAKE_KEEP_APP_ID=1 on its own private session bus."""
    environ = os.environ if environ is None else environ
    return not fake or environ.get("CHIPPY_FAKE_KEEP_APP_ID") == "1"


def extensions_root() -> Path:
    if in_flatpak():  # exposed by --filesystem=xdg-data/gnome-shell/extensions:create
        return Path.home() / ".local" / "share" / "gnome-shell" / "extensions"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "gnome-shell" / "extensions"


# Where an installed Chippy keeps its copy: Flatpak, then distro packages, then local installs.
INSTALLED_DIRS = ("/app/share", "/usr/share", "/usr/local/share")


def bundled_source(installed_dirs=INSTALLED_DIRS) -> Path:
    override = os.environ.get("CHIPPY_EXTENSION_DIR")
    if override:
        return Path(override)
    for base in installed_dirs:
        installed = Path(base) / "chippy" / "extension" / UUID
        if installed.is_dir():
            return installed
    return Path(__file__).resolve().parents[2] / "extension" / UUID


def _version(folder: Path) -> int:
    try:
        return int(json.loads((folder / "metadata.json").read_text()).get("version", 0))
    except (OSError, ValueError, TypeError):
        return 0


class ShellExtensions:
    """org.gnome.Shell.Extensions over the session bus."""

    def __init__(self):
        self._proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
            None,
            "org.gnome.Shell.Extensions",
            "/org/gnome/Shell/Extensions",
            "org.gnome.Shell.Extensions",
            None,
        )

    def _call(self, method: str, uuid: str):
        return self._proxy.call_sync(
            method, GLib.Variant("(s)", (uuid,)), Gio.DBusCallFlags.NONE, 5000, None
        ).unpack()[0]

    def enable(self, uuid: str) -> bool:
        return bool(self._call("EnableExtension", uuid))

    def disable(self, uuid: str) -> bool:
        return bool(self._call("DisableExtension", uuid))

    def known(self, uuid: str) -> bool:
        return bool(self._call("GetExtensionInfo", uuid))


class Installer:
    def __init__(self, source: Path, root: Path, shell):
        self._source, self._root, self._shell = Path(source), Path(root), shell
        self.target = self._root / UUID

    def installed(self) -> bool:
        return (
            self.target.is_dir()
            and not self.target.is_symlink()
            and (self.target / MARKER).exists()
        )

    def _check_ours(self) -> None:
        if self.target.is_symlink():
            raise ShellExtError(f"{self.target} is a symlink; Chippy won't touch it")
        if self.target.exists() and not (self.target / MARKER).exists():
            raise ShellExtError(f"{self.target} wasn't installed by Chippy; not touching it")

    def install(self) -> bool:
        self._check_ours()
        self._root.mkdir(parents=True, exist_ok=True)
        staging = self._root / f".{UUID}.chippy-tmp"
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(self._source, staging, symlinks=False)
        (staging / MARKER).write_text(
            "Installed by Chippy; removed when you turn off 'Show in top bar'.\n"
        )
        if self.target.exists():
            shutil.rmtree(self.target)
        staging.rename(self.target)
        try:
            relogin = not self._shell.known(UUID)
            self._shell.enable(UUID)
        except GLib.Error:
            relogin = True
        return relogin

    def update_if_needed(self) -> bool:
        if self.installed() and _version(self._source) > _version(self.target):
            self.install()
            return True
        return False

    def remove(self) -> None:
        self._check_ours()  # refuse before touching the Shell's state for a foreign folder
        try:
            self._shell.disable(UUID)
        except GLib.Error:
            pass
        if not self.target.exists() and not self.target.is_symlink():
            return
        shutil.rmtree(self.target)
