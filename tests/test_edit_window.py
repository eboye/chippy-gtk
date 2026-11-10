import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# A session bus that activates nothing: no portals or keyring daemons start for the test.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.mark.skipif(
    not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")), reason="needs a display"
)
@pytest.mark.skipif(shutil.which("dbus-run-session") is None, reason="needs dbus-run-session")
def test_edit_mode_in_the_main_window(tmp_path):
    """Enter, save, discard, auto-leave, merge, split and a refused kind (edit_window_flow.py),
    in a child process on a private session bus."""
    config = tmp_path / "bus.conf"
    config.write_text(BUS_CONFIG.format(dir=tmp_path))
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        "ADW_DISABLE_PORTAL": "1",
        "GDK_DEBUG": "no-portals",
        "GSETTINGS_BACKEND": "memory",
        "GTK_A11Y": "none",
    }
    env.pop("CHIPPY_FAKE_KEEP_APP_ID", None)
    done = subprocess.run(
        [
            "dbus-run-session",
            f"--config-file={config}",
            "--",
            sys.executable,
            "-m",
            "tests.edit_window_flow",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "ok", done.stdout
    assert done.stderr.strip() == "", done.stderr
