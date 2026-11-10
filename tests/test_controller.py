import pytest
from gi.repository import GLib

from chippy.controller import OFFLINE_ERROR, ThreadRunner, VacuumController, next_interval
from chippy.core.fake import FakeVacuum, build_fake_payload
from chippy.core.profiles import R2209
from chippy.store import MapCache, RoomStore


class InlineRunner:
    def run(self, func, callback):
        try:
            result, error = func(), None
        except Exception as err:  # noqa: BLE001
            result, error = None, err
        callback(result, error)

    def shutdown(self):
        pass


class ManualScheduler:
    def __init__(self):
        self.pending = []

    def call_later(self, seconds, fn, name="poll"):
        self.pending.append((seconds, fn, name))
        return len(self.pending)

    def cancel(self, handle):
        self.pending[handle - 1] = (None, None, None)

    def armed(self, name):
        return [p for p in self.pending if p[1] and p[2] == name]

    def fire_named(self, name):
        """Fire the latest pending timer called `name`; drop the older ones. Returns its delay."""
        seconds, fn, _ = self.armed(name)[-1]
        self.pending = [(None, None, None) if p[2] == name else p for p in self.pending]
        fn()
        return seconds

    def fire(self):
        return self.fire_named("poll")


@pytest.fixture
def parts(tmp_path):
    return dict(
        rooms=RoomStore(tmp_path / "rooms.json", legacy=tmp_path / "none.json"),
        map_cache=MapCache(tmp_path / "map.payload"),
    )


def make(parts, vacuum=None, fetch=build_fake_payload, clock=lambda: 0.0):
    sched = ManualScheduler()
    ctl = VacuumController(
        vacuum or FakeVacuum(),
        R2209,
        parts["rooms"],
        parts["map_cache"],
        fetch,
        runner=InlineRunner(),
        scheduler=sched,
        clock=clock,
    )
    events = []
    for sig in ("snapshot-changed", "rooms-changed", "map-changed"):
        ctl.connect(sig, lambda *_a, s=sig: events.append(s))
    for sig in ("action-failed", "map-failed"):
        ctl.connect(sig, lambda _c, msg, s=sig: events.append((s, msg)))
    ctl.connect("notify-user", lambda _c, t, b: events.append(("notify", t, b)))
    return ctl, sched, events


def test_next_interval():
    assert next_interval("docked", 0) == 15
    assert next_interval("cleaning", 0) == 5
    assert next_interval("returning", 2) == 5
    assert next_interval("docked", 3) == 60


def test_start_polls_and_loads_the_map(parts):
    ctl, sched, events = make(parts)
    ctl.start()
    assert ctl.snapshot.state == "docked"
    assert "snapshot-changed" in events and "map-changed" in events
    assert {r.id for r in ctl.visible_rooms()} == {1, 2, 3, 4}  # 5 is tiny → hidden once
    assert parts["rooms"].hidden == {5}
    assert parts["map_cache"].load() == build_fake_payload()
    assert sched.pending[-1][0] == 15


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (0, []),
        (68, [("reminder", "Mopping done: clean the mop")]),
        (11, [("attention", "Dust bin full")]),
        (47, [("error", "Robot is blocked")]),
    ],
)
def test_notices_carry_fault_severity(parts, fault, expected):
    ctl, _sched, _events = make(parts, vacuum=FakeVacuum(fault=fault))
    ctl.start()
    assert ctl.notices() == expected


def test_cached_map_shows_before_the_cloud(parts):
    parts["map_cache"].save(build_fake_payload())

    def failing():
        raise RuntimeError("cloud down")

    ctl, _, events = make(parts, fetch=failing)
    ctl.start()
    assert ctl.grid is not None
    assert ("map-failed", "cloud down") in events


def test_offline_after_three_failures(parts):
    class Dead(FakeVacuum):
        def poll(self):
            raise OSError("timeout")

    ctl, sched, _ = make(parts, vacuum=Dead())
    ctl.start()
    assert ctl.snapshot.state == "offline" or ctl.failures == 1
    sched.fire()
    sched.fire()
    assert ctl.failures == 3 and ctl.snapshot.state == "offline"
    assert ctl.snapshot.error == OFFLINE_ERROR
    assert sched.pending[-1][0] == 60


def test_actions_poke_and_report_errors(parts):
    ctl, sched, events = make(parts)
    ctl.start()
    ctl.act("start")
    assert ctl.snapshot.state == "cleaning"
    assert sched.pending[-1][0] == 5
    done = []
    ctl.act("set_suction", "nope", on_done=done.append)
    assert isinstance(done[0], ValueError)
    # the poke after the failure may add a snapshot-changed, so look at the failure events only
    failures = [e for e in events if isinstance(e, tuple) and e[0] == "action-failed"]
    assert len(failures) == 1 and "nope" in failures[0][1]
    assert failures[0][1].startswith("Set suction failed")


def test_clean_rooms_uses_current_levels(parts):
    seen = {}

    class Spy(FakeVacuum):
        def clean_rooms(self, ids, suction, water):
            seen.update(ids=ids, suction=suction, water=water)
            super().clean_rooms(ids, suction, water)

    ctl, _, _ = make(parts, vacuum=Spy())
    ctl.start()
    ctl.clean_rooms([2, 1])
    assert seen == {"ids": [2, 1], "suction": "standard", "water": "medium"}


def test_finished_notification(parts):
    ctl, sched, events = make(parts)
    ctl.start()
    ctl.act("start")
    ctl.act("dock")
    for _ in range(4):
        sched.fire()
    assert any(
        e[0] == "notify" and e[1] == "Cleaning finished" for e in events if isinstance(e, tuple)
    )


def test_rename_and_hide(parts):
    ctl, _, events = make(parts)
    ctl.start()
    events.clear()
    ctl.rename_room(1, "Lounge")
    ctl.set_room_hidden(2, True)
    assert [r.name for r in ctl.visible_rooms()] == ["Lounge", "Bedroom", "Hallway"]
    assert events.count("rooms-changed") == 2


def test_encrypted_map_profile_does_not_fetch(parts):
    from dataclasses import replace

    calls = []
    ctl, _, events = make(parts, fetch=lambda: calls.append(1) or b"")
    ctl.profile = replace(R2209, map_format="encrypted")
    ctl.refresh_map()
    assert calls == []
    assert events[-1] == ("map-failed", "This model's map can't be shown yet")


def _run_on_main_loop(func):
    runner = ThreadRunner()
    got = []
    ctx = GLib.MainContext.default()
    timed_out = []
    timeout_id = GLib.timeout_add_seconds(5, lambda: timed_out.append(1) or GLib.SOURCE_REMOVE)
    try:
        runner.run(func, lambda result, error: got.append((result, error)))
        for _ in range(200):
            if got or timed_out:
                break
            ctx.iteration(True)
    finally:
        if not timed_out:
            GLib.source_remove(timeout_id)
        runner.shutdown()
    return got


def test_thread_runner_delivers_result_on_main_loop():
    assert _run_on_main_loop(lambda: 41 + 1) == [(42, None)]


def test_thread_runner_delivers_error_on_main_loop():
    def boom():
        raise ValueError("bad")

    got = _run_on_main_loop(boom)
    assert len(got) == 1
    result, error = got[0]
    assert result is None and isinstance(error, ValueError) and str(error) == "bad"


class DeferredRunner:
    def __init__(self):
        self.jobs = []

    def run(self, func, callback):
        self.jobs.append((func, callback))

    def shutdown(self):
        pass


def test_late_results_after_stop_are_ignored(parts):
    runner = DeferredRunner()
    ctl = VacuumController(
        FakeVacuum(),
        R2209,
        parts["rooms"],
        parts["map_cache"],
        build_fake_payload,
        runner=runner,
        scheduler=ManualScheduler(),
    )
    events = []
    for sig in ("snapshot-changed", "rooms-changed", "map-changed"):
        ctl.connect(sig, lambda *_a, s=sig: events.append(s))
    ctl.connect("action-failed", lambda *_a: events.append("action-failed"))
    ctl.start()
    ctl.act("start", on_done=lambda err: events.append("on_done"))
    ctl.stop()
    jobs, runner.jobs = runner.jobs, []
    for func, callback in jobs:
        try:
            callback(func(), None)
        except Exception as err:  # noqa: BLE001
            callback(None, err)
    assert events == ["on_done"]
    assert parts["map_cache"].load() is None
    late = []
    ctl.act("start", on_done=late.append)
    ctl.clean_rooms([1], on_done=late.append)
    assert [str(e) for e in late] == ["Chippy has stopped"] * 2
    ctl.refresh_map()
    ctl.poke()
    assert runner.jobs == []


def test_thread_runner_shutdown_does_not_log_cancelled_jobs(caplog):
    import threading

    gate = threading.Event()
    runner = ThreadRunner()
    for _ in range(3):  # 2 workers busy + queued jobs that get cancelled
        runner.run(lambda: gate.wait(2), lambda *_: None)
    runner.shutdown()
    gate.set()
    ctx = GLib.MainContext.default()
    for _ in range(20):
        ctx.iteration(False)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# live position ------------------------------------------------------------
def test_live_updates_only_while_cleaning(parts):
    from chippy.core.fake import fake_live_payload

    steps = iter(range(100))
    ctl, sched, events = make(parts)
    ctl._fetch_live = lambda: fake_live_payload(next(steps))
    ctl.connect("live-changed", lambda *_a: events.append("live-changed"))
    ctl.start()
    assert ctl.live is None and sched.armed("live") == []
    ctl.act("start")  # → cleaning → live timer armed
    assert len(sched.armed("live")) == 1
    sched.fire()  # polls while the live timer is armed must not add a second one
    sched.fire()
    assert len(sched.armed("live")) == 1
    sched.fire_named("live")
    assert ctl.live is not None and ctl.live.robot is not None
    first = ctl.live.robot
    sched.fire_named("live")
    assert ctl.live.robot != first
    ctl.act("stop")  # idle → live cleared
    assert ctl.live is None and events.count("live-changed") >= 3
    assert sched.armed("live") == []


def test_live_backs_off_after_empty_answers(parts):
    ctl, sched, _ = make(parts)
    ctl._fetch_live = lambda: None
    ctl.start()
    ctl.act("start")
    delays = [sched.fire_named("live") for _ in range(4)]
    assert delays[:3] == [5, 5, 5] and delays[3] == 30
    assert len(sched.armed("live")) == 1
    ctl.stop()
    assert sched.armed("live") == []


def test_live_failures_count_as_empty_and_success_resets(parts):
    from chippy.core.fake import fake_live_payload

    answers = [b"junk", RuntimeError("cloud down"), None, fake_live_payload(0), None]

    def fetch():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    ctl, sched, events = make(parts)
    ctl._fetch_live = fetch
    ctl.start()
    ctl.act("start")
    delays = [sched.fire_named("live") for _ in range(5)]
    assert delays == [5, 5, 5, 30, 5]  # 3 failures → back off; a frame resets the count
    assert ctl.live is None  # the last answer was empty: no stale position
    assert sched.armed("live")[-1][0] == 5
    assert not [e for e in events if isinstance(e, tuple)]  # no toasts for live failures


def test_live_disabled_without_fetcher_map_or_plain_format(parts):
    from dataclasses import replace

    ctl, sched, _ = make(parts)  # no fetch_live_payload
    ctl.start()
    ctl.act("start")
    assert sched.armed("live") == []

    ctl, sched, _ = make(parts)
    ctl._fetch_live = lambda: None
    ctl.profile = replace(R2209, map_format="encrypted")
    ctl.start()
    ctl.act("start")
    assert sched.armed("live") == []


def test_live_waits_for_a_saved_map(parts, tmp_path):
    from chippy.store import MapCache

    def no_map():
        raise RuntimeError("cloud down")

    parts["map_cache"] = MapCache(tmp_path / "empty.payload")
    ctl, sched, _ = make(parts, fetch=no_map)
    ctl._fetch_live = lambda: None
    ctl.start()
    ctl.act("start")
    assert ctl.grid is None and sched.armed("live") == []


def test_live_result_after_leaving_cleaning_or_stop_is_ignored(parts):
    from chippy.core.fake import fake_live_payload

    runner = DeferredRunner()
    sched = ManualScheduler()
    ctl = VacuumController(
        FakeVacuum(),
        R2209,
        parts["rooms"],
        parts["map_cache"],
        build_fake_payload,
        runner=runner,
        scheduler=sched,
        fetch_live_payload=lambda: fake_live_payload(0),
    )
    events = []
    ctl.connect("live-changed", lambda *_a: events.append("live-changed"))

    def drain():
        jobs, runner.jobs = runner.jobs, []
        for func, callback in jobs:
            try:
                callback(func(), None)
            except Exception as err:  # noqa: BLE001
                callback(None, err)

    ctl.start()
    drain()
    ctl.act("start")
    drain()  # action → poke
    drain()  # poll → cleaning → live armed
    sched.fire_named("live")  # live fetch in flight
    sched.fire()  # a poll while the answer is out must not arm a second live timer
    in_flight = [j for j in runner.jobs if j[0].__name__ == "work_live"]
    runner.jobs = [j for j in runner.jobs if j[0].__name__ != "work_live"]
    drain()
    assert ctl.snapshot.state == "cleaning" and sched.armed("live") == []
    runner.jobs = in_flight
    ctl.act("stop")
    jobs = runner.jobs
    runner.jobs = [j for j in jobs if j[0].__name__ != "work_live"]
    drain()
    drain()  # idle → live loop stopped
    runner.jobs = [j for j in jobs if j[0].__name__ == "work_live"]
    drain()  # the stale live answer arrives
    assert ctl.live is None and events == []

    ctl.act("start")
    drain()
    drain()
    sched.fire_named("live")
    ctl.stop()
    drain()
    assert ctl.live is None and events == []
    assert sched.armed("live") == []


# expired Xiaomi session ------------------------------------------------------
def test_expired_session_from_live_stops_cloud_work_but_not_polls(parts):
    from chippy.core.cloud import CloudSessionExpired

    calls = {"map": 0, "live": 0}

    def fetch_map():
        calls["map"] += 1
        return build_fake_payload()

    def fetch_live():
        calls["live"] += 1
        raise CloudSessionExpired()

    vacuum = FakeVacuum()
    polls = []
    real_poll = vacuum.poll
    vacuum.poll = lambda: polls.append(1) or real_poll()
    ctl, sched, events = make(parts, vacuum=vacuum, fetch=fetch_map)
    ctl._fetch_live = fetch_live
    expired = []
    ctl.connect("cloud-expired", lambda *_a: expired.append(1))
    ctl.start()
    ctl.act("start")
    assert len(sched.armed("live")) == 1
    sched.fire_named("live")
    assert expired == [1] and ctl.cloud_expired
    assert sched.armed("live") == [] and ctl.live is None  # no backoff retries
    before = len(polls)
    for _ in range(3):
        sched.fire()  # polling over miIO goes on, still cleaning
    assert len(polls) == before + 3 and ctl.snapshot.state == "cleaning"
    assert sched.armed("live") == []  # the live loop is not re-armed
    ctl.refresh_map()
    assert calls == {"map": 1, "live": 1}  # no further cloud fetches
    assert expired == [1]
    assert not [e for e in events if isinstance(e, tuple) and e[0] == "map-failed"]


def test_expired_session_from_map_refresh_is_signalled_once(parts):
    from chippy.core.cloud import CloudSessionExpired

    calls = []

    def fetch_map():
        calls.append(1)
        raise CloudSessionExpired()

    ctl, sched, events = make(parts, fetch=fetch_map)
    ctl._fetch_live = lambda: pytest.fail("live fetch after expiry")
    expired = []
    ctl.connect("cloud-expired", lambda *_a: expired.append(1))
    ctl.start()
    ctl.refresh_map()
    ctl.refresh_map()
    assert calls == [1] and expired == [1]
    ctl.act("start")
    assert sched.armed("live") == []
    sched.fire()
    assert ctl.snapshot.state == "cleaning"


def test_device_and_cloud_work_use_separate_runners(parts):
    device, cloud = DeferredRunner(), DeferredRunner()
    ctl = VacuumController(
        FakeVacuum(),
        R2209,
        parts["rooms"],
        parts["map_cache"],
        build_fake_payload,
        runner=device,
        cloud_runner=cloud,
        scheduler=ManualScheduler(),
    )
    ctl.start()
    ctl.act("start")
    assert [f.__name__ for f, _ in cloud.jobs] == ["work"]  # the map
    assert len(device.jobs) == 2  # the poll and the action
    stopped = []
    device.shutdown = lambda: stopped.append("device")
    cloud.shutdown = lambda: stopped.append("cloud")
    ctl.stop()
    assert stopped == ["device", "cloud"]


def test_a_stuck_cloud_request_does_not_block_polls():
    import threading

    device, cloud = ThreadRunner(name="t-device"), ThreadRunner(name="t-cloud")
    gate = threading.Event()
    try:
        for _ in range(4):  # more stuck cloud jobs than cloud workers
            cloud.run(lambda: gate.wait(10), lambda *_: None)
        got = []
        device.run(lambda: "polled", lambda result, error: got.append(result))
        ctx = GLib.MainContext.default()
        for _ in range(200):
            if got:
                break
            ctx.iteration(True)
        assert got == ["polled"]
    finally:
        gate.set()
        device.shutdown()
        cloud.shutdown()


def test_thread_runner_shutdown_returns_with_a_job_stuck_forever():
    import threading
    import time

    runner = ThreadRunner()
    started = threading.Event()
    runner.run(lambda: started.set() or threading.Event().wait(), lambda *_: None)
    assert started.wait(5)
    t0 = time.monotonic()
    runner.shutdown()
    runner.shutdown()  # idempotent
    assert time.monotonic() - t0 < 0.5
    runner.run(lambda: pytest.fail("ran after shutdown"), lambda *_: None)


def test_process_with_a_stuck_runner_job_exits_promptly():
    import subprocess
    import sys

    script = (
        "import threading\n"
        "from chippy.controller import ThreadRunner\n"
        "runner = ThreadRunner()\n"
        "started = threading.Event()\n"
        "runner.run(lambda: started.set() or threading.Event().wait(), lambda *_: None)\n"
        "assert started.wait(5)\n"
        "runner.shutdown()\n"
        "print('exiting')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "exiting"
