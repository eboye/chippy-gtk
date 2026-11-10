"""Non-secret local data: room names/hidden rooms, settings, cached map."""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "chippy"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "chippy"


def legacy_names_path() -> Path:
    # The host path, not XDG_CONFIG_HOME: inside Flatpak that points into ~/.var/app.
    return Path.home() / ".config" / "dreamed" / "room-names.json"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_doc(path: Path) -> tuple[str, dict]:
    """Classify a JSON object file: ("absent" | "ok" | "corrupt" | "unreadable", doc)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return "absent", {}
    except OSError:
        return "unreadable", {}
    except ValueError:  # undecodable bytes
        return "corrupt", {}
    try:
        data = json.loads(text)
    except ValueError:
        return "corrupt", {}
    if not isinstance(data, dict):
        return "corrupt", {}
    return "ok", data


def _int_names(data: object) -> dict[int, str]:
    if not isinstance(data, dict):
        return {}
    return {
        int(k): v.strip()
        for k, v in data.items()
        if isinstance(k, str) and k.isascii() and k.isdigit() and isinstance(v, str) and v.strip()
    }


class RoomStore:
    def __init__(self, path: Path | None = None, legacy: Path | None = None):
        self._path = path or config_dir() / "rooms.json"
        status, data = _read_doc(self._path)
        if status == "corrupt":
            # Keep the unreadable bytes for inspection; then start as on a first run.
            try:
                os.replace(self._path, self._path.with_name(self._path.name + ".corrupt"))
            except OSError:
                status = "unreadable"  # could not move it aside: never overwrite it
            data = {}
        self.names: dict[int, str] = _int_names(data.get("names"))
        hidden = data.get("hidden")
        self.hidden: set[int] = (
            {int(i) for i in hidden if type(i) is int} if isinstance(hidden, list) else set()
        )
        self._seeded = data.get("seeded") is True
        self._imported = data.get("imported") is True
        if status == "unreadable":
            # Existing file we cannot read: don't import over it and don't save now.
            self._imported = False
            return
        if not self._imported:
            self._import_legacy(legacy or legacy_names_path())
            self._save()

    def _import_legacy(self, legacy: Path) -> None:
        # Marked done only if the legacy file parsed as an object; else a later start retries.
        status, doc = _read_doc(legacy)
        if status != "ok":
            return
        self.names = {**_int_names(doc), **self.names}
        self._imported = True

    def rename(self, seg_id: int, name: str) -> None:
        if name.strip():
            self.names[seg_id] = name.strip()
        else:
            self.names.pop(seg_id, None)
        self._save()

    def replace_names(self, names: dict[int, str]) -> None:
        self.names = {seg: n.strip() for seg, n in names.items() if n.strip()}
        self._save()

    def set_hidden(self, seg_id: int, hidden: bool) -> None:
        (self.hidden.add if hidden else self.hidden.discard)(seg_id)
        self._save()

    def seed_hidden(self, ids: set[int]) -> bool:
        if self._seeded:
            return False
        self.hidden |= set(ids)
        self._seeded = True
        self._save()
        return True

    def _save(self) -> None:
        doc = {
            "names": {str(k): v for k, v in sorted(self.names.items())},
            "hidden": sorted(self.hidden),
            "seeded": self._seeded,
            "imported": self._imported,
        }
        _write_atomic(self._path, json.dumps(doc, indent=2).encode())


@dataclass
class Settings:
    background: bool = True
    autostart: bool = True
    top_bar: bool = False
    signed_out: bool = False  # user signed out: don't re-import the dreamed keyring item
    path: Path = field(default=Path(), repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_dir() / "settings.json"
        data = _read_json(path)
        values = {
            f.name: data[f.name]
            for f in fields(cls)
            if f.name != "path" and isinstance(data.get(f.name), bool)
        }
        return cls(**values, path=path)

    def save(self) -> None:
        doc = {k: v for k, v in asdict(self).items() if k != "path"}
        _write_atomic(self.path, json.dumps(doc, indent=2).encode())


class MapCache:
    def __init__(self, path: Path | None = None):
        self._path = path or cache_dir() / "map.payload"

    def load(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except OSError:
            return None

    def save(self, payload: bytes) -> None:
        _write_atomic(self._path, payload)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
