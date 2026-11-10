#!/usr/bin/env bash
# Start the installed Chippy with its demo robot on a virtual display and check that the main
# window comes up with the demo flat's rooms on the map. Needs Xvfb and dbus-run-session.
# Usage: packaging/smoke-test.sh
set -euo pipefail

if [ "${1:-}" != "--inner" ]; then
    smoke_dir="$(mktemp -d)"
    export SMOKE_DIR="$smoke_dir"
    Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >"$smoke_dir/xvfb.log" 2>&1 &
    xvfb_pid=$!
    trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT
    export DISPLAY=:99 GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none
    chippy --help >/dev/null
    smoke_rc=0
    dbus-run-session -- "$0" --inner || smoke_rc=$?
    echo "--- app log"
    cat "$smoke_dir/app.log"
    exit "$smoke_rc"
fi

state_file="$SMOKE_DIR/state.json"
chippy --fake --test-state "$state_file" >"$SMOKE_DIR/app.log" 2>&1 &
app_pid=$!
smoke_ok=0
for _ in $(seq 1 120); do
    if ! kill -0 "$app_pid" 2>/dev/null; then
        echo "Chippy exited early" >&2
        break
    fi
    if [ -f "$state_file" ] && python3 -c '
import json, sys
state = json.load(open(sys.argv[1]))
ok = state.get("page") == "main" and len(state.get("room_names") or {}) >= 4 and state.get("rooms")
sys.exit(0 if ok else 1)
' "$state_file"; then
        smoke_ok=1
        break
    fi
    sleep 0.5
done
kill "$app_pid" 2>/dev/null || true
wait "$app_pid" 2>/dev/null || true
[ -f "$state_file" ] && echo "--- state" && cat "$state_file" && echo
if [ "$smoke_ok" != 1 ]; then
    echo "FAIL: the main window with the demo rooms never showed up" >&2
    exit 1
fi
if grep -q 'Traceback' "$SMOKE_DIR/app.log"; then
    echo "FAIL: Chippy logged a Python traceback" >&2
    exit 1
fi
echo "PASS: Chippy shows the demo flat"
