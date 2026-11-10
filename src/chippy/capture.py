"""`chippy --capture-live DIR`: record the robot's live map frames while the user cleans.

Every `interval` seconds for `minutes`, read the robot's recent property 6.1 values from the
Xiaomi cloud (the partial map frames it uploads while cleaning; see `core/live.py`). It sends
no robot command. New rows go to `rows.jsonl` as they were stored; `log.txt` gets one line per
round, so the live format can be checked against the decoder.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .core.live import HISTORY_LIMIT, LIVE_PROPERTY, frames_in

LOOKBACK = 120  # s of history read in the first round


def run_capture(
    creds,
    profile,
    directory,
    minutes: float = 3,
    interval: float = 5,
    *,
    history=None,
    clock=time.time,
    sleep=time.sleep,
) -> int:
    """Capture live frames into `directory`; returns the process exit code.
    `history(start, end) -> [(time, value)]` defaults to the robot's 6.1 history in the cloud."""
    out = Path(directory)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"chippy: {out} is not an empty directory; refusing to capture", file=sys.stderr)
        return 2
    if history is None:
        from .core.cloud import CloudMap

        cloud_map = CloudMap(creds)

        def history(start, end):
            return cloud_map.property_history(creds.did, LIVE_PROPERTY, start, end, HISTORY_LIMIT)

    out.mkdir(parents=True, exist_ok=True)
    rounds = max(1, int(minutes * 60 // interval))
    seen: set[tuple[int, str]] = set()
    since = int(clock()) - LOOKBACK
    with open(out / "log.txt", "a") as log, open(out / "rows.jsonl", "a") as rows_file:
        for index in range(1, rounds + 1):
            if index > 1:
                sleep(interval)
            now = int(clock())
            line, since = _capture_one(history, since, now, seen, rows_file)
            log.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
            log.flush()
            print(line, file=sys.stderr)
    return 0


def _capture_one(history, since: int, now: int, seen: set, rows_file) -> tuple[str, int]:
    """One round; the log columns are `new-rows frames robot path-points`, or an error."""
    try:
        rows = history(since, now + 60)
    except Exception as err:  # noqa: BLE001 - record it and keep capturing
        return f"history-failed {type(err).__name__}: {err}", since
    fresh = [(t, v) for t, v in rows if (t, v) not in seen]
    frames, robot = 0, "-"
    path = 0
    for when, value in sorted(fresh, key=lambda r: r[0]):
        seen.add((when, value))
        rows_file.write(json.dumps({"time": when, "value": value}) + "\n")
        for frame in frames_in(value):
            frames += 1
            path += len(frame.path)
            if frame.robot:
                robot = ",".join(str(v) for v in frame.robot)
    rows_file.flush()
    latest = max((t for t, _ in rows), default=since)
    return f"{len(fresh)} {frames} {robot} {path}", latest
