import json

from chippy.core.credentials import (
    ATTRS,
    LEGACY_ATTRS,
    Credentials,
    MemoryCollection,
    delete,
    load,
    save,
)

CREDS = Credentials(
    ip="10.0.0.2",
    token="t" * 32,
    did="1",
    model="dreame.vacuum.r2209",
    country="de",
    username="u@example.com",
    password="pw",
    cloud_session={"user_id": "1"},
)


def test_round_trip():
    col = MemoryCollection()
    save(CREDS, col)
    assert load(col) == CREDS
    assert col.items[0].attributes == ATTRS


def test_imports_the_dreamed_item_once_and_leaves_it_alone():
    col = MemoryCollection()
    col.create_item(
        "Dreame vacuum (dreamed)",
        dict(LEGACY_ATTRS),
        json.dumps({**CREDS.__dict__, "future_field": 1}).encode(),
    )
    assert load(col) == CREDS
    assert [i.attributes for i in col.items] == [LEGACY_ATTRS, ATTRS]
    assert load(col) == CREDS and len(col.items) == 2


def test_none_when_empty():
    assert load(MemoryCollection()) is None


def test_legacy_not_imported_when_disabled():
    col = MemoryCollection()
    col.create_item("old", dict(LEGACY_ATTRS), json.dumps(CREDS.__dict__).encode())
    assert load(col, import_legacy=False) is None
    assert len(col.items) == 1


def test_delete_removes_only_chippy_items():
    col = MemoryCollection()
    col.create_item("old", dict(LEGACY_ATTRS), b"{}")
    save(CREDS, col)
    delete(col)
    assert [i.attributes for i in col.items] == [LEGACY_ATTRS]
