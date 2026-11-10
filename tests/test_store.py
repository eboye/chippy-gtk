import json
from pathlib import Path

from chippy.store import MapCache, RoomStore, Settings, config_dir, legacy_names_path


def test_room_store_imports_dreamed_names_once(tmp_path):
    legacy = tmp_path / "room-names.json"
    legacy.write_text(json.dumps({"1": "Bathroom", "7": "Studio", "x": "bad"}))
    store = RoomStore(tmp_path / "rooms.json", legacy=legacy)
    assert store.names == {1: "Bathroom", 7: "Studio"}
    legacy.write_text(json.dumps({"1": "Changed"}))
    assert RoomStore(tmp_path / "rooms.json", legacy=legacy).names == {1: "Bathroom", 7: "Studio"}


def test_rename_hide_seed(tmp_path):
    path = tmp_path / "rooms.json"
    store = RoomStore(path, legacy=tmp_path / "missing.json")
    store.rename(2, "  Kitchen ")
    store.rename(3, "x")
    store.rename(3, "   ")  # empty name removes the override
    assert store.seed_hidden({6}) is True
    assert store.seed_hidden({9}) is False  # only once, ever
    store.set_hidden(6, False)
    store.set_hidden(5, True)
    again = RoomStore(path, legacy=tmp_path / "missing.json")
    assert (again.names, again.hidden) == ({2: "Kitchen"}, {5})


def test_corrupt_room_file_starts_empty(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text("{nope")
    store = RoomStore(path, legacy=tmp_path / "missing.json")
    assert (store.names, store.hidden) == ({}, set())


def test_settings_round_trip_and_defaults(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings.load(path)
    assert (s.background, s.autostart, s.top_bar, s.signed_out) == (True, True, False, False)
    s.top_bar = True
    s.save()
    assert Settings.load(path).top_bar is True
    path.write_text('{"background": "yes", "unknown": 1}')
    assert Settings.load(path).background is True  # wrong types fall back to defaults


def test_map_cache(tmp_path):
    cache = MapCache(tmp_path / "map.payload")
    assert cache.load() is None
    cache.save(b"abc")
    assert cache.load() == b"abc"
    cache.clear()
    assert cache.load() is None


def test_default_paths(tmp_path):
    assert config_dir() == tmp_path / "config" / "chippy"
    assert legacy_names_path() == tmp_path / "home" / ".config" / "dreamed" / "room-names.json"


def test_corrupt_room_file_is_moved_aside(tmp_path):
    path = tmp_path / "rooms.json"
    corrupt = tmp_path / "rooms.json.corrupt"
    corrupt.write_text("older corrupt copy")  # an older .corrupt is replaced
    legacy = tmp_path / "room-names.json"
    legacy.write_text(json.dumps({"1": "Bathroom"}))
    path.write_text("{nope")
    store = RoomStore(path, legacy=legacy)
    assert corrupt.read_text() == "{nope"
    assert json.loads(path.read_text())["imported"] is True
    assert store.names == {1: "Bathroom"}


def test_unreadable_room_file_is_left_alone(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    path.write_text(json.dumps({"names": {"2": "Kitchen"}, "imported": True}))
    legacy = tmp_path / "room-names.json"
    legacy.write_text(json.dumps({"1": "Bathroom"}))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    real_read_text = Path.read_text

    def deny(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    store = RoomStore(path, legacy=legacy)
    assert (store.names, store.hidden) == ({}, set())  # no import from legacy either
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_missing_legacy_is_retried_on_a_later_start(tmp_path):
    path = tmp_path / "rooms.json"
    legacy = tmp_path / "room-names.json"
    store = RoomStore(path, legacy=legacy)
    assert json.loads(path.read_text())["imported"] is False
    store.rename(2, "Kitchen")
    legacy.write_text(json.dumps({"1": "Bathroom", "2": "Legacy"}))
    again = RoomStore(path, legacy=legacy)
    assert again.names == {1: "Bathroom", 2: "Kitchen"}  # existing rename wins
    assert json.loads(path.read_text())["imported"] is True


def test_hidden_rejects_json_booleans(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text(json.dumps({"hidden": [True, 3], "imported": True}))
    assert RoomStore(path, legacy=tmp_path / "missing.json").hidden == {3}
