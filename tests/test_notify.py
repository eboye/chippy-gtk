from chippy.core.notify import Notifier
from chippy.core.state import Snapshot


def s(state, **kw):
    return Snapshot(state=state, **kw)


def test_clean_finished_once():
    n = Notifier(frozenset({8, 9, 10, 11}))
    assert n.events(s("docked"), s("cleaning"), 0) == []
    assert n.events(s("cleaning"), s("returning", cleaned_area=24, cleaning_time=31), 1) == []
    events = n.events(s("returning", cleaned_area=24, cleaning_time=31), s("charging"), 2)
    assert events == [("Cleaning finished", "24 m² in 31 min")]
    assert n.events(s("charging"), s("docked"), 3) == []


def test_error_transition():
    n = Notifier(frozenset({8, 9, 10, 11}))
    err = s("error", fault=47, error="Robot is blocked")
    assert n.events(s("cleaning"), err, 0) == [("Vacuum needs help", "Robot is blocked")]
    assert n.events(err, err, 1) == []


def test_error_during_clean_then_dock_is_not_a_finished_clean():
    n = Notifier(frozenset({8, 9, 10, 11}))
    err = s("error", fault=47, error="Robot is blocked")
    n.events(s("docked"), s("cleaning"), 0)
    assert n.events(s("cleaning"), err, 1) == [("Vacuum needs help", "Robot is blocked")]
    assert n.events(err, s("idle"), 2) == []
    assert n.events(s("idle"), s("charging"), 3) == []


def test_attention_fault_without_error_state():
    n = Notifier(frozenset({8, 9, 10, 11}))
    full = s("docked", fault=11, error="Dust bin full")
    assert n.events(s("docked"), full, 0) == [("Vacuum needs attention", "Dust bin full")]
    assert n.events(full, full, 1) == []


def test_reminder_fault_sends_a_reminder_once():
    n = Notifier(frozenset({8, 9, 10, 11}), frozenset({68}))
    mop = s("charging", fault=68, error="Mopping done: clean the mop")
    events = n.events(s("charging"), mop, 0)
    assert events == [("Vacuum reminder", "Mopping done: clean the mop")]
    assert n.events(mop, mop, 1) == []


def test_offline_during_clean_after_ten_minutes():
    n = Notifier(frozenset({8, 9, 10, 11}))
    n.events(s("docked"), s("cleaning"), 0)
    assert n.events(s("cleaning"), s("offline"), 100) == []
    assert n.events(s("offline"), s("offline"), 650) == []
    assert n.events(s("offline"), s("offline"), 701) == [
        ("Vacuum is offline", "Lost contact for over 10 minutes during a clean")
    ]
    assert n.events(s("offline"), s("offline"), 900) == []


def test_offline_while_idle_is_silent():
    n = Notifier(frozenset({8, 9, 10, 11}))
    assert n.events(s("docked"), s("offline"), 0) == []
    assert n.events(s("offline"), s("offline"), 5000) == []
