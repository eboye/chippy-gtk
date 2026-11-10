import json

from chippy.capture import run_capture
from chippy.core.profiles import R2209
from tests.test_live import _frame, _value


class _History:
    """The cloud's 6.1 history: each call returns the rows stored so far, or raises."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_capture_writes_rows_and_a_log(tmp_path):
    out = tmp_path / "cap"
    row1 = (1000, _value(_frame((10, 20, 90), "S10,20L5,0")))
    row2 = (1005, _value(_frame((30, 40, 180), "l30,40")))
    history = _History([[row1], RuntimeError("timeout"), [row1, row2], [row1, row2]])
    sleeps = []
    code = run_capture(
        None,
        R2209,
        out,
        minutes=20 / 60,
        interval=5,
        history=history,
        clock=lambda: 1010,
        sleep=sleeps.append,
    )
    assert code == 0 and sleeps == [5] * 3
    assert history.calls[0] == (1010 - 120, 1070)
    assert history.calls[-1][0] == 1005  # later rounds start at the newest row
    rows = [json.loads(line) for line in (out / "rows.jsonl").read_text().splitlines()]
    assert [r["time"] for r in rows] == [1000, 1005]  # each row once
    lines = [line.split(" ", 1)[1] for line in (out / "log.txt").read_text().splitlines()]
    assert lines[0] == "1 1 10,20,90 2"
    assert "timeout" in lines[1]
    assert lines[2] == "1 1 30,40,180 1"
    assert lines[3] == "0 0 - 0"


def test_capture_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "old.bin").write_bytes(b"x")
    history = _History([])
    assert run_capture(None, R2209, tmp_path, history=history, sleep=lambda s: None) != 0
    assert history.calls == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.bin"]


def test_capture_refuses_a_regular_file(tmp_path):
    target = tmp_path / "file.bin"
    target.write_bytes(b"keep")
    history = _History([])
    assert run_capture(None, R2209, target, history=history, sleep=lambda s: None) != 0
    assert history.calls == [] and target.read_bytes() == b"keep"


def test_capture_option_refuses_fake_mode(tmp_path, capsys):
    from chippy.app import _capture_live

    assert _capture_live(str(tmp_path / "cap"), fake=True) == 2
    assert "real robot" in capsys.readouterr().err
    assert not (tmp_path / "cap").exists()
