import json
import os
import subprocess
import sys
import time

import pytest

needs_display = pytest.mark.skipif(
    not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")), reason="needs a display"
)
TEST_APP_ID = "--gapplication-app-id=io.github.eboye.ChippyTest"


def _env(tmp_path, **extra):
    env = {
        **os.environ,
        "CHIPPY_FAKE_HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        **extra,
    }
    env.pop("CHIPPY_FAKE_KEEP_APP_ID", None)  # never the real app id on the live session bus
    return env


def _state_when(tmp_path, done, **extra_env):
    """Run the fake app until its test state satisfies `done`; return the last state."""
    state = tmp_path / "state.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "chippy", "--fake", "--test-state", str(state), TEST_APP_ID],
        env=_env(tmp_path, **extra_env),
    )
    try:
        data = {}
        for _ in range(100):
            if state.exists():
                data = json.loads(state.read_text())
                if done(data):
                    break
            time.sleep(0.1)
        assert state.exists(), f"the app never wrote {state} (exit code {proc.poll()})"
        return data
    finally:
        proc.terminate()
        proc.wait(5)


@needs_display
def test_fake_app_starts_and_writes_state(tmp_path):
    data = _state_when(tmp_path, lambda d: d.get("page") == "main")
    assert data.get("page") == "main", data
    assert data.get("app_state") == "ready", data
    assert data.get("cloud_banner") is None, data


@needs_display
def test_fake_app_reports_edit_mode_and_the_map_zones(tmp_path):
    data = _state_when(tmp_path, lambda d: d.get("page") == "main" and d.get("rooms"))
    edit = data.get("edit")
    assert edit is not None, data
    assert edit["mode"] is False and edit["tool"] is None and edit["dirty"] is False, edit
    assert edit["zones"] == {"no_go": 0, "no_mop": 0, "walls": 0}, edit
    assert edit["buttons"]["edit"] is not None, edit
    assert edit["buttons"]["select"] is None, edit  # the toolbar shows only in edit mode
    assert edit["alert"] is None, edit


@needs_display
def test_expired_xiaomi_session_shows_the_sign_in_banner(tmp_path):
    data = _state_when(
        tmp_path, lambda d: d.get("cloud_banner") is not None, CHIPPY_FAKE_CLOUD_EXPIRED="1"
    )
    assert data.get("cloud_banner") == [
        "Xiaomi sign-in expired — map updates are paused",
        "app.sign-in",
    ], data
    assert data.get("page") == "main" and data.get("app_state") == "ready", data


@needs_display
def test_fake_background_run_does_not_stay_behind(tmp_path):
    """Background mode is on by default; a fake started hidden must still exit by itself."""
    done = subprocess.run(
        [sys.executable, "-m", "chippy", "--fake", "--background", TEST_APP_ID],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 0, done.stderr
