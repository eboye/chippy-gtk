import pytest

from chippy.core.fake import FakeVacuum


def test_fake_lifecycle():
    vac = FakeVacuum()
    assert vac.poll().state == "docked"
    vac.start()
    first = vac.poll()
    assert first.state == "cleaning"
    second = vac.poll()
    assert second.cleaned_area > first.cleaned_area
    vac.pause()
    assert vac.poll().state == "paused"
    vac.dock()
    assert vac.poll().state == "returning"
    assert vac.poll().state == "charging"


def test_fake_settings_and_rooms():
    vac = FakeVacuum()
    vac.set_suction("turbo")
    vac.set_water("high")
    snap = vac.poll()
    assert (snap.suction, snap.water, snap.has_water_tank) == ("turbo", "high", True)
    vac.clean_rooms([16], "turbo", "high")
    assert vac.poll().state == "cleaning"
    with pytest.raises(ValueError):
        vac.set_suction("max")


def test_fake_map_object():
    assert FakeVacuum().request_map_object() == "fake-map"
