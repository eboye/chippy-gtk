"""The one owner of the robot: polling, actions, map refresh, notifications."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from gi.repository import GLib, GObject
from miio.exceptions import DeviceException

from .core.cloud import CloudSessionExpired
from .core.mapdecode import MapFrame, decode_map
from .core.notify import Notifier
from .core.state import Snapshot
from .mapmodel import LiveState, build_rooms, grid_from_frame, live_from_frame, tiny_rooms

log = logging.getLogger(__name__)

OFFLINE_AFTER = 3
OFFLINE_ERROR = "Can't reach the robot (Wi-Fi asleep, wrong IP, or the token changed)"
ENCRYPTED_MAP = "This model's map can't be shown yet"
_ACTIVE = {"cleaning", "returning"}
LIVE_INTERVAL = 5  # s between live position requests while cleaning or returning
LIVE_BACKOFF = 30  # s between requests after LIVE_EMPTY_LIMIT empty answers in a row
LIVE_EMPTY_LIMIT = 3
_TRANSPORT_ERRORS = (DeviceException, OSError, TimeoutError)


def next_interval(state: str, failures: int) -> int:
    if failures >= OFFLINE_AFTER:
        return 60
    return 5 if state in _ACTIVE else 15


class ThreadRunner:
    """Run blocking calls off the main loop; deliver (result, error) back on it.

    The workers are daemon threads: a request that never returns must not keep Chippy alive
    after quit (concurrent.futures joins its workers at interpreter exit)."""

    def __init__(self, max_workers: int = 2, name: str = "chippy"):
        self._max_workers, self._name = max_workers, name
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def run(self, func, callback) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._threads:  # started on first use, all at once
                for index in range(self._max_workers):
                    worker = threading.Thread(
                        target=self._work, name=f"{self._name}-{index}", daemon=True
                    )
                    self._threads.append(worker)
                    worker.start()
            self._jobs.put((func, callback))

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None or self._closed:
                return  # shut down: queued jobs are dropped, like cancelled futures
            func, callback = job
            try:
                result, error = func(), None
            except Exception as err:  # noqa: BLE001 - delivered to the callback
                result, error = None, err
            del job, func  # don't pin the job's closure while waiting for the next one

            def deliver(callback=callback, result=result, error=error):
                callback(result, error)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(deliver)

    def shutdown(self) -> None:
        """Drop queued jobs and return at once; a running job finishes (or hangs) on its own
        daemon thread and its result is still delivered."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._jobs.put(None)


class GLibScheduler:
    def call_later(self, seconds: float, fn, name: str = "poll") -> int:
        def fire():
            fn()
            return GLib.SOURCE_REMOVE

        return GLib.timeout_add(int(seconds * 1000), fire)

    def cancel(self, handle: int) -> None:
        GLib.source_remove(handle)


class VacuumController(GObject.Object):
    __gsignals__ = {
        "snapshot-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "rooms-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "map-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "action-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "map-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "live-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "notify-user": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # the saved Xiaomi session was refused: map and live fetches stop until a new sign-in
        "cloud-expired": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        vacuum,
        profile,
        rooms,
        map_cache,
        fetch_map_payload: Callable[[], bytes],
        runner=None,
        scheduler=None,
        clock=time.monotonic,
        fetch_live_payload: Callable[[], MapFrame | bytes | None] | None = None,
        cloud_runner=None,
        fetch_cleanset: Callable[[], object | None] | None = None,
    ):
        super().__init__()
        self._vacuum, self.profile = vacuum, profile
        self._room_store, self._map_cache, self._fetch = rooms, map_cache, fetch_map_payload
        # Device work (polls, actions) and cloud work (map, live) run on separate pools, so a
        # slow Xiaomi request never delays a poll or a button press. A test's  serves
        # both unless it passes  too.
        self._runner = runner or ThreadRunner(name="chippy-device")
        self._cloud_runner = cloud_runner or runner or ThreadRunner(name="chippy-cloud")
        self.cloud_expired = False
        self._scheduler = scheduler or GLibScheduler()
        self._clock = clock
        self._notifier = Notifier(profile.attention_faults, profile.reminder_faults)
        self.snapshot = Snapshot()
        self.failures = 0
        self.grid = None
        self.cloud_names: dict[int, str] = {}
        self.rooms = []
        self._timer: int | None = None
        self._running = False
        self._stopped = False
        self._in_flight = False
        self._poke_pending = False
        # live position while cleaning: None disables it
        self._fetch_live = fetch_live_payload
        self._fetch_cleanset = fetch_cleanset
        self.live: LiveState | None = None
        self._frame = None  # the saved map's frame: its geometry places live positions
        self._live_timer: int | None = None
        self._live_running = False  # timer armed or a request in flight
        self._live_generation = 0  # bumped when the loop stops; older answers are dropped
        self._live_empty = 0

    # lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._stopped = False
        cached = self._map_cache.load()
        if cached:
            try:
                self._set_frame(decode_map(cached))
            except Exception as err:  # noqa: BLE001 - a bad cache is just ignored
                log.warning("cached map unreadable: %s", err)
        self._rebuild_rooms()
        self._running = True
        self._poll_now()
        self.refresh_map()

    def stop(self) -> None:
        self._running = False
        self._stopped = True
        if self._timer is not None:
            self._scheduler.cancel(self._timer)
            self._timer = None
        self._stop_live(emit=False)
        self._runner.shutdown()
        self._cloud_runner.shutdown()

    # polling ---------------------------------------------------------------
    def poke(self) -> None:
        if not self._running:
            return
        if self._timer is not None:
            self._scheduler.cancel(self._timer)
            self._timer = None
        self._poll_now()

    def _poll_now(self) -> None:
        if self._in_flight:
            self._poke_pending = True
            return
        self._in_flight = True
        self._runner.run(self._vacuum.poll, self._on_poll)

    def _on_poll(self, snap, error) -> None:
        self._in_flight = False
        if self._stopped:
            return
        if error is not None:
            self.failures += 1
            if isinstance(error, _TRANSPORT_ERRORS):
                log.warning("poll failed (%d): %s", self.failures, error)
            else:
                log.error("poll failed unexpectedly (%d)", self.failures, exc_info=error)
            if self.failures < OFFLINE_AFTER:
                self._schedule()
                return
            snap = replace(self.snapshot, state="offline", error=OFFLINE_ERROR)
        else:
            self.failures = 0
        prev, self.snapshot = self.snapshot, snap
        if prev != snap:
            self.emit("snapshot-changed")
        if prev != snap or snap.state == "offline":
            try:
                for title, body in self._notifier.events(prev, snap, self._clock()):
                    self.emit("notify-user", title, body)
            except Exception:  # noqa: BLE001 - a broken handler must not stop polling
                log.exception("notification handler failed")
        self._update_live()
        self._schedule()

    def _schedule(self) -> None:
        if not self._running:
            return
        if self._poke_pending:
            self._poke_pending = False
            self._poll_now()
            return
        self._timer = self._scheduler.call_later(
            next_interval(self.snapshot.state, self.failures), self._tick
        )

    def _tick(self) -> None:
        self._timer = None
        self._poll_now()

    # actions ---------------------------------------------------------------
    def act(self, name: str, *args, on_done=None) -> None:
        if self._stopped:
            if on_done is not None:
                on_done(RuntimeError("Chippy has stopped"))
            return
        method = getattr(self._vacuum, name)

        def done(_result, error):
            if self._stopped:
                if on_done is not None:
                    on_done(error or RuntimeError("Chippy has stopped"))
                return
            if error is not None:
                label = name.replace("_", " ").capitalize()
                self.emit("action-failed", f"{label} failed: {error}")
            if on_done is not None:
                on_done(error)
            self.poke()

        self._runner.run(lambda: method(*args), done)

    def clean_rooms(self, ids, on_done=None) -> None:
        self.act(
            "clean_rooms",
            list(ids),
            self.snapshot.suction or "standard",
            self.snapshot.water or "medium",
            on_done=on_done,
        )

    # live position -------------------------------------------------------
    def _live_enabled(self) -> bool:
        return (
            self._fetch_live is not None
            and self.profile.map_format == "plain"
            and self._frame is not None
            and self.grid is not None
            and not self.cloud_expired
        )

    def _update_live(self) -> None:
        """Start the live loop when a clean begins, stop it (and drop the position) after."""
        if self.snapshot.state in _ACTIVE and self._live_enabled():
            if not self._live_running:
                self._live_running = True
                self._live_empty = 0
                self._arm_live(LIVE_INTERVAL)
        elif self._live_running or self.live is not None:
            self._stop_live()

    def _arm_live(self, seconds: int) -> None:
        self._live_timer = self._scheduler.call_later(seconds, self._live_tick, name="live")

    def _stop_live(self, emit: bool = True) -> None:
        if self._live_timer is not None:
            self._scheduler.cancel(self._live_timer)
            self._live_timer = None
        self._live_running = False
        self._live_generation += 1
        if self.live is not None:
            self.live = None
            if emit:
                self.emit("live-changed")

    def _live_tick(self) -> None:
        self._live_timer = None
        if self._stopped:
            return
        generation, fetch = self._live_generation, self._fetch_live

        def work_live():
            result = fetch()  # a decoded frame, or the raw payload of one
            if not result:
                return None
            return result if isinstance(result, MapFrame) else decode_map(result)

        self._cloud_runner.run(
            work_live, lambda frame, error: self._on_live(generation, frame, error)
        )

    def _on_live(self, generation: int, frame, error) -> None:
        if self._stopped or generation != self._live_generation:
            return  # Chippy stopped, or the clean ended while this request was out
        if isinstance(error, CloudSessionExpired):
            self._cloud_session_expired()
            return
        live = None
        if error is not None:
            log.warning("live position unavailable: %s", error)
        elif frame is not None:
            live = live_from_frame(frame, self._frame, self.grid)
        found = live is not None and live.robot is not None
        self._live_empty = 0 if found else self._live_empty + 1  # no position counts as empty
        if live != self.live:
            self.live = live
            self.emit("live-changed")
        backoff = self._live_empty >= LIVE_EMPTY_LIMIT
        self._arm_live(LIVE_BACKOFF if backoff else LIVE_INTERVAL)

    # map and rooms ---------------------------------------------------------
    def refresh_map(self) -> None:
        if self._stopped or self.cloud_expired:
            return
        if self.profile.map_format != "plain":
            self.emit("map-failed", ENCRYPTED_MAP)
            return

        def work():
            payload = self._fetch()
            return payload, decode_map(payload)

        self._cloud_runner.run(work, self._on_map)

    def _on_map(self, result, error) -> None:
        if self._stopped:
            return
        if isinstance(error, CloudSessionExpired):
            self._cloud_session_expired()
            return
        if error is not None:
            log.warning("map refresh failed: %s", error)
            self.emit("map-failed", str(error))
            return
        payload, frame = result
        self._map_cache.save(payload)
        self._set_frame(frame)

    def _cloud_session_expired(self) -> None:
        """Stop every cloud fetch (polling the robot goes on) and tell the user once."""
        if self.cloud_expired:
            return
        log.warning("Xiaomi session expired; map updates paused until the next sign-in")
        self.cloud_expired = True
        self._stop_live()
        self.emit("cloud-expired")

    def _set_frame(self, frame) -> None:
        self._frame = frame
        self.grid = grid_from_frame(frame)
        self.cloud_names = dict(frame.names)
        self._room_store.seed_hidden(tiny_rooms(self.grid))
        self._rebuild_rooms(force=True)
        self.emit("map-changed")

    def _rebuild_rooms(self, force: bool = False) -> None:
        rooms = build_rooms(
            self.grid, self.cloud_names, self._room_store.names, self._room_store.hidden
        )
        if force or rooms != self.rooms:
            self.rooms = rooms
            self.emit("rooms-changed")

    def visible_rooms(self):
        return [r for r in self.rooms if not r.hidden]

    def notices(self) -> list[tuple[str, str]]:
        """The robot's current messages as (severity, text); the robot reports one fault
        at a time, so this holds at most one for now."""
        snap = self.snapshot
        if not snap.fault or snap.state == "offline":
            return []
        return [(self.profile.fault_severity(snap.fault), snap.error)]

    def rename_room(self, seg_id: int, name: str) -> None:
        self._room_store.rename(seg_id, name)
        self._rebuild_rooms()

    def set_room_hidden(self, seg_id: int, hidden: bool) -> None:
        self._room_store.set_hidden(seg_id, hidden)
        self._rebuild_rooms()
