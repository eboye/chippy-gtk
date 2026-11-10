"""The robot's live position, followed through the map frames it uploads while it cleans.

The r2209 answers the map request (action 6.1) with an empty object name. Instead, every few
seconds of a clean it writes a small partial map frame to MIoT property 6.1, and the Xiaomi
cloud keeps those values: `[time, '["<url-safe base64 zlib frame>", ...]']`. Each frame
carries the robot's pose in its header and the newest stretch of its path in `tr`.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from .mapdecode import MapDecodeError, MapFrame, decode_map, frame_trailer

log = logging.getLogger(__name__)

LIVE_PROPERTY = "6.1"
LOOKBACK = 30 * 60  # s of history read when following starts (Chippy opened mid-clean)
RUN_GAP = 120  # s without a frame that separate two cleans: older frames are dropped
RESTART_GAP = 60  # s between polls after which following starts over
HISTORY_LIMIT = 300
CLEANSET_LOOKBACK_DAYS = 7

History = Callable[[int, int], list[tuple[int, str]]]  # (start, end) -> [(time, value)]


def frames_in(value: str) -> list[MapFrame]:
    """The frames in one 6.1 value; undecodable ones are skipped."""
    try:
        items = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        items = [value]
    if not isinstance(items, list):
        items = [items]
    frames = []
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            frames.append(decode_map(item))
        except MapDecodeError as err:
            log.debug("skipping a live frame: %s", err)
    return frames


def latest_cleanset(
    history: History, now: int, days: int = CLEANSET_LOOKBACK_DAYS
) -> object | None:
    """The per-room settings in the newest frame that carries them, or None."""
    for _when, value in sorted(
        history(now - days * 86400, now + 60), key=lambda r: r[0], reverse=True
    ):
        try:
            items = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            items = [value]
        if not isinstance(items, list):
            items = [items]
        for item in reversed(items):
            if isinstance(item, str):
                cleanset = frame_trailer(item).get("cleanset")
                if cleanset is not None:
                    return cleanset
    return None


class LiveTrack:
    """Polls the 6.1 history and keeps the robot's latest pose plus the path of this clean."""

    def __init__(self, history: History, clock: Callable[[], float] = time.time):
        self._history = history
        self._clock = clock
        self._last_poll: float | None = None
        self._reset(0)

    def _reset(self, since: int) -> None:
        self._since = since
        self._seen: set[tuple[int, str]] = set()
        self._last_time: int | None = None
        self._latest: MapFrame | None = None
        self._path: list[tuple[int, int]] = []

    def poll(self) -> MapFrame | None:
        """The latest live frame with the whole path so far, or None when there is none."""
        now = int(self._clock())
        if self._last_poll is None or now - self._last_poll > RESTART_GAP:
            self._reset(now - LOOKBACK)
        self._last_poll = now
        rows = self._history(self._since, now + 60)
        for when, value in sorted(rows, key=lambda r: r[0]):
            key = (when, value)
            if key in self._seen or (self._last_time is not None and when < self._last_time):
                continue
            if self._last_time is not None and when - self._last_time > RUN_GAP:
                self._path, self._latest = [], None  # a new clean: forget the old path
            self._seen.add(key)
            self._last_time = when
            for frame in frames_in(value):
                self._path.extend(frame.path)
                if frame.robot is not None:
                    self._latest = frame
        if self._last_time is not None:
            self._since = self._last_time  # the same second again; `_seen` drops repeats
            self._seen = {k for k in self._seen if k[0] >= self._last_time}
        if self._latest is None:
            return None
        if now - (self._last_time or 0) > RUN_GAP:
            return None  # nothing recent: the robot isn't uploading (yet)
        return replace(self._latest, path=list(self._path))
