import base64
import json
import struct
import zlib

from chippy.core.live import RUN_GAP, LiveTrack, frames_in


def _frame(robot, tr="", frame_id=1):
    header = struct.pack("<hhBhhhhhhhhhhh", 2, frame_id, 80, *robot, 0, 0, 0, 50, 0, 0, 0, 0)
    trailer = json.dumps({"tr": tr, "fsm": 1}).encode()
    return base64.urlsafe_b64encode(zlib.compress(header + trailer)).decode().rstrip("=")


def _value(*frames):
    return json.dumps(list(frames))


class _History:
    def __init__(self):
        self.rows: list[tuple[int, str]] = []
        self.calls: list[tuple[int, int]] = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        return [r for r in self.rows if start <= r[0] <= end]


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_frames_in_skips_junk():
    good = _frame((10, 20, 90))
    assert [f.robot for f in frames_in(_value(good, "!!", 5))] == [(10, 20, 90)]
    assert frames_in("not json") == []


def test_follows_pose_and_builds_the_path_across_frames():
    history, clock = _History(), _Clock(1000)
    track = LiveTrack(history, clock)
    assert track.poll() is None  # nothing uploaded yet
    history.rows += [(995, _value(_frame((1, 0, 0), "S0,0L50,0")))]
    clock.now = 1005
    first = track.poll()
    assert first.robot == (1, 0, 0) and first.path == [(0, 0), (50, 0)]
    history.rows += [(1004, _value(_frame((100, 50, 90), "l50,0L50,50", 2)))]
    clock.now = 1010
    second = track.poll()
    assert second.robot == (100, 50, 90)
    assert second.path == [(0, 0), (50, 0), (50, 0), (100, 50)]
    clock.now = 1015
    assert track.poll().path == second.path  # rows seen again are not added twice
    assert history.calls[-1][0] == 1004  # later polls only ask for recent rows


def test_an_older_clean_is_dropped():
    history, clock = _History(), _Clock(5000)
    history.rows += [(1000, _value(_frame((1, 1, 0), "S1,1")))]  # yesterday's clean
    history.rows += [(4990, _value(_frame((7, 7, 0), "S7,7L1,0")))]
    live = LiveTrack(history, clock).poll()
    assert live.robot == (7, 7, 0) and live.path == [(7, 7), (8, 7)]


def test_stale_frames_mean_no_position():
    history, clock = _History(), _Clock(5000)
    history.rows += [(5000 - RUN_GAP - 10, _value(_frame((7, 7, 0))))]
    assert LiveTrack(history, clock).poll() is None


def test_a_long_pause_between_polls_starts_over():
    history, clock = _History(), _Clock(1000)
    track = LiveTrack(history, clock)
    history.rows += [(999, _value(_frame((5, 5, 0), "S5,5")))]
    assert track.poll().path == [(5, 5)]
    clock.now = 3000  # the next clean, much later
    history.rows += [(2999, _value(_frame((9, 9, 0), "S9,9")))]
    assert track.poll().path == [(9, 9)]
