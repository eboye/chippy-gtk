"""Robot token and Xiaomi login, kept in GNOME Keyring only."""

import json
from dataclasses import asdict, dataclass, field, fields

ATTRS = {"application": "chippy"}
LEGACY_ATTRS = {"application": "dreamed"}
LABEL = "Chippy robot vacuum"


@dataclass
class Credentials:
    ip: str
    token: str
    did: str
    model: str
    country: str
    username: str = ""
    password: str = ""
    # Xiaomi session (user_id, service_token, ssecurity) reused so map downloads don't need a
    # fresh password login, which Xiaomi answers with email 2FA.
    cloud_session: dict = field(default_factory=dict)


class KeyringLocked(Exception):
    """The login keyring is locked and the unlock prompt was dismissed."""


def default_collection():
    import secretstorage

    conn = secretstorage.dbus_init()
    collection = secretstorage.get_default_collection(conn)
    # secretstorage's unlock() returns True when the user dismissed the prompt.
    if collection.is_locked() and collection.unlock():
        raise KeyringLocked("the GNOME keyring is locked (unlock prompt dismissed)")
    return collection


def _parse(secret: bytes) -> Credentials:
    data = json.loads(secret)
    known = {f.name for f in fields(Credentials)}
    return Credentials(**{k: v for k, v in data.items() if k in known})


def save(creds: Credentials, collection) -> None:
    secret = json.dumps(asdict(creds)).encode()
    collection.create_item(LABEL, dict(ATTRS), secret, replace=True)


def load(collection, *, import_legacy: bool = True) -> Credentials | None:
    for item in collection.search_items(dict(ATTRS)):
        return _parse(item.get_secret())
    if import_legacy:
        for item in collection.search_items(dict(LEGACY_ATTRS)):
            creds = _parse(item.get_secret())
            save(creds, collection)
            return creds
    return None


def delete(collection) -> None:
    for item in list(collection.search_items(dict(ATTRS))):
        item.delete()


class _MemoryItem:
    def __init__(self, owner, label, attributes, secret):
        self._owner, self.label, self.attributes, self._secret = owner, label, attributes, secret

    def get_secret(self) -> bytes:
        return self._secret

    def delete(self) -> None:
        self._owner.items.remove(self)


class MemoryCollection:
    """In-memory stand-in for a Secret Service collection (tests and --fake)."""

    def __init__(self):
        self.items: list[_MemoryItem] = []

    def search_items(self, attributes):
        return [
            i for i in self.items if all(i.attributes.get(k) == v for k, v in attributes.items())
        ]

    def create_item(self, label, attributes, secret, replace=True):
        if replace:
            for item in self.search_items(attributes):
                if item.attributes == attributes:
                    self.items.remove(item)
        item = _MemoryItem(self, label, dict(attributes), bytes(secret))
        self.items.append(item)
        return item
