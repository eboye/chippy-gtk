#!/usr/bin/env bash
#
# Run Chippy (`--fake`) + the Chippy extension + the dev probe in an ISOLATED
# headless GNOME Shell. The probe drives the app window and the top-bar
# indicator with real (virtual) pointer and keyboard input and takes screenshots.
#
# Usage: tools/nested-check.sh [--scenario main|signin|qr|edit] [--scheme light|dark] [--timeout SECONDS]
# Output: ${CN_NEST_ROOT:-/tmp/chippy-nest}/out/{results.txt,*.png,shell.log,app.log,state.json}
# Exit:   0 only if every required probe check passed and the logs have no extension
#         errors, no app tracebacks and no app GLib criticals/warnings (a11y noise aside).
#
# Isolation: XDG dirs and DCONF_PROFILE are exported BEFORE dbus-run-session, so
# the session bus and the dconf service it activates never see the real
# config. The run aborts if the isolated dconf db is not created, and the real
# session's enabled-extensions is compared (and restored) on exit.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
nest_root="${CN_NEST_ROOT:-/tmp/chippy-nest}"
ext_uuid="chippy@eboye.github.io"
probe_uuid="chippy-probe@eboye.github.io"

case "$nest_root" in
    *..*) echo "CN_NEST_ROOT must not contain '..': $nest_root" >&2; exit 2 ;;
    /tmp/chippy-nest*) ;;
    *) echo "CN_NEST_ROOT must be an absolute path matching /tmp/chippy-nest*: $nest_root" >&2; exit 2 ;;
esac

if [ "${1:-}" = "--inner" ]; then
    resolved_root="${CN_NEST_ROOT_RESOLVED:?}"
    case "${DCONF_PROFILE:-}" in "$resolved_root"/*) ;; *) echo "isolation check failed: DCONF_PROFILE=${DCONF_PROFILE:-}" >&2; exit 1 ;; esac
    case "${XDG_CONFIG_HOME:-}" in "$resolved_root"/*) ;; *) echo "isolation check failed: XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-}" >&2; exit 1 ;; esac
    dconf write /org/chippy-nest/canary true
    if [ ! -f "$XDG_CONFIG_HOME/dconf/cnnest" ]; then
        env -u DCONF_PROFILE XDG_CONFIG_HOME="$CN_ORIG_CONFIG_HOME" dconf reset /org/chippy-nest/canary || true
        echo "dconf isolation check failed: canary write did not reach $XDG_CONFIG_HOME/dconf/cnnest; reverted canary" >&2
        exit 1
    fi
    dconf write /org/gnome/shell/enabled-extensions "['$ext_uuid', '$probe_uuid']"
    dconf write /org/gnome/shell/welcome-dialog-last-shown-version "'999'"
    scheme_value=default
    [ "$CN_SCHEME" = dark ] && scheme_value=prefer-dark
    dconf write /org/gnome/desktop/interface/color-scheme "'$scheme_value'"
    case "$CN_SCENARIO" in signin|qr) export CHIPPY_FAKE_SIGNED_OUT=1 ;; esac
    if [ "$CN_SCENARIO" = qr ]; then export CHIPPY_FAKE_QR_DELAY=3; fi
    export CHIPPY_FAKE_HOME="$CN_NEST_ROOT_RESOLVED/fake-home"
    # On this private bus the fake keeps the real app id, so the extension finds it.
    export CHIPPY_FAKE_KEEP_APP_ID=1
    (
        for _ in $(seq 1 100); do
            [ -S "$XDG_RUNTIME_DIR/chippy-nest" ] && break
            sleep 0.2
        done
        export WAYLAND_DISPLAY=chippy-nest GDK_BACKEND=wayland
        if [ "$CN_APP" = flatpak ]; then
            # The installed Flatpak (fake mode). Its user installation lives under the real
            # data dir, which the isolated XDG_DATA_HOME hides; it only gets the nest dir.
            export FLATPAK_USER_DIR="$CN_FLATPAK_USER_DIR"
            app_env=(--env=CHIPPY_FAKE_KEEP_APP_ID=1 "--env=CHIPPY_FAKE_HOME=$CHIPPY_FAKE_HOME")
            for app_var in CHIPPY_FAKE_SIGNED_OUT CHIPPY_FAKE_FAULT CHIPPY_FAKE_QR_DELAY; do
                [ -n "${!app_var:-}" ] && app_env+=("--env=$app_var=${!app_var}")
            done
            exec flatpak run --user "${app_env[@]}" "--filesystem=$CN_NEST_ROOT_RESOLVED" \
                io.github.eboye.Chippy --fake --test-state "$CN_PROBE_OUT/state.json"
        fi
        export CHIPPY_EXTENSION_DIR="$CN_REPO_ROOT/extension/chippy@eboye.github.io"
        # The venv binary, not `uv run`: the isolated XDG_CACHE_HOME would make uv resync.
        exec "$CN_REPO_ROOT/.venv/bin/chippy" --fake --test-state "$CN_PROBE_OUT/state.json"
    ) > "$CN_PROBE_OUT/app.log" 2>&1 &
    exec gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x800 --wayland-display chippy-nest
fi

run_timeout=150
scenario=main
scheme=light
while [ $# -gt 0 ]; do
    case "$1" in
        --timeout) run_timeout="$2"; shift 2 ;;
        --scenario) scenario="$2"; shift 2 ;;
        --scheme) scheme="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$scenario" in main|signin|qr|edit) ;; *) echo "--scenario must be main, signin, qr or edit: $scenario" >&2; exit 2 ;; esac
case "$scheme" in light|dark) ;; *) echo "--scheme must be light or dark: $scheme" >&2; exit 2 ;; esac
export CN_SCENARIO="$scenario" CN_SCHEME="$scheme"

# CN_APP=flatpak tests the installed Flatpak instead of the source tree (e.g. on a machine
# with another GNOME version and no dev setup); the extension under test is still this repo's.
export CN_APP="${CN_APP:-venv}"
export CN_FLATPAK_USER_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/flatpak"
if [ "$CN_APP" = flatpak ]; then
    if ! flatpak info --user io.github.eboye.Chippy >/dev/null 2>&1; then
        echo "CN_APP=flatpak but io.github.eboye.Chippy isn't installed for this user" >&2
        exit 2
    fi
elif [ ! -x "$repo_root/.venv/bin/chippy" ]; then
    echo ".venv/bin/chippy missing; run 'uv sync' first" >&2
    exit 2
fi

orig_config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
orig_runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
real_gsettings() {
    env -u DCONF_PROFILE XDG_CONFIG_HOME="$orig_config_home" XDG_RUNTIME_DIR="$orig_runtime_dir" gsettings "$@"
}
real_before="$(real_gsettings get org.gnome.shell enabled-extensions)"

unmount_fuse() {
    fusermount3 -u "$nest_root/run/gvfs" 2>/dev/null || true
    fusermount3 -u "$nest_root/run/doc" 2>/dev/null || true
}

nest_pid=""
cleanup() {
    if [ -n "$nest_pid" ]; then
        kill -TERM -- "-$nest_pid" 2>/dev/null || true
        pkill -TERM -s "$nest_pid" 2>/dev/null || true
        sleep 1
        kill -KILL -- "-$nest_pid" 2>/dev/null || true
        pkill -KILL -s "$nest_pid" 2>/dev/null || true
    fi
    rm -f "$nest_root/session.pid"
    unmount_fuse
    real_after="$(real_gsettings get org.gnome.shell enabled-extensions)"
    if [ "$real_after" != "$real_before" ]; then
        # Only undo a change that looks like a leak from this run: the extension
        # or the probe uuid appeared (a leaked enable) or disappeared (a leaked
        # disable). Changes to any other uuid are the user's own edit and are
        # left alone. Uuids are matched with their quotes.
        leaked=0
        list_body="${real_after#@as }"
        list_body="${list_body#[}"
        list_body="${list_body%]}"
        list_items=()
        while [ -n "$list_body" ]; do
            list_item="${list_body%%,*}"
            if [ "$list_item" = "$list_body" ]; then list_body=""; else list_body="${list_body#*,}"; fi
            list_item="${list_item#"${list_item%%[![:space:]]*}"}"
            list_item="${list_item%"${list_item##*[![:space:]]}"}"
            [ -n "$list_item" ] && list_items+=("$list_item")
        done
        for nest_uuid in "$ext_uuid" "$probe_uuid"; do
            in_before=0
            in_after=0
            case "$real_before" in *"'$nest_uuid'"*) in_before=1 ;; esac
            case "$real_after" in *"'$nest_uuid'"*) in_after=1 ;; esac
            if [ "$in_before" = 0 ] && [ "$in_after" = 1 ]; then
                leaked=1
                kept_items=()
                for list_item in "${list_items[@]}"; do
                    [ "$list_item" = "'$nest_uuid'" ] || kept_items+=("$list_item")
                done
                list_items=("${kept_items[@]}")
            elif [ "$in_before" = 1 ] && [ "$in_after" = 0 ]; then
                leaked=1
                list_items+=("'$nest_uuid'")
            fi
        done
        if [ "$leaked" = 1 ]; then
            if [ "${#list_items[@]}" -eq 0 ]; then
                restored_list="@as []"
            else
                restored_list="[$(printf '%s, ' "${list_items[@]}")"
                restored_list="${restored_list%, }]"
            fi
            echo "REAL SESSION enabled-extensions gained or lost a nest uuid; restoring: $restored_list" >&2
            real_gsettings set org.gnome.shell enabled-extensions "$restored_list"
        else
            echo "warning: real enabled-extensions changed during the run (not by the nest); left as is" >&2
        fi
    fi
}
trap cleanup EXIT

if [ -f "$nest_root/session.pid" ]; then
    stale_sid="$(cat "$nest_root/session.pid")"
    case "$stale_sid" in
        ''|*[!0-9]*) ;;
        *)
            # Kill only a real leftover nest: sid > 1, not our own session, and
            # the session leader (pid == sid) is running this script's --inner stage.
            own_sid="$(ps -o sid= -p $$ | tr -d '[:space:]')"
            leader_sid="$(ps -o sid= -p "$stale_sid" 2>/dev/null | tr -d '[:space:]' || true)"
            leader_cmdline=""
            if [ -r "/proc/$stale_sid/cmdline" ]; then
                leader_cmdline="$(tr '\0' ' ' < "/proc/$stale_sid/cmdline" 2>/dev/null || true)"
            fi
            if [ "$stale_sid" -gt 1 ] && [ "$stale_sid" != "$own_sid" ] \
                && [ "$leader_sid" = "$stale_sid" ] \
                && case "$leader_cmdline" in *"nested-check.sh --inner"*) true ;; *) false ;; esac; then
                pkill -TERM -s "$stale_sid" 2>/dev/null || true
                sleep 1
            else
                echo "note: skipping stale session.pid ($stale_sid): not a chippy-nest session" >&2
            fi
            ;;
    esac
fi
unmount_fuse
rm -rf "$nest_root"

export XDG_CONFIG_HOME="$nest_root/config"
export XDG_DATA_HOME="$nest_root/data"
export XDG_CACHE_HOME="$nest_root/cache"
export XDG_STATE_HOME="$nest_root/state"
export XDG_RUNTIME_DIR="$nest_root/run"
export DCONF_PROFILE="$nest_root/dconf-profile"
export CN_PROBE_OUT="$nest_root/out"
export CN_NEST_ROOT_RESOLVED="$nest_root"
export CN_ORIG_CONFIG_HOME="$orig_config_home"
export CN_REPO_ROOT="$repo_root"

mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$CN_PROBE_OUT"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
printf 'user-db:cnnest\n' > "$DCONF_PROFILE"

ext_root="$XDG_DATA_HOME/gnome-shell/extensions"
mkdir -p "$ext_root"
cp -r "$repo_root/extension/$ext_uuid" "$ext_root/$ext_uuid"
cp -r "$repo_root/tools/probe/$probe_uuid" "$ext_root/"

setsid dbus-run-session -- bash "$0" --inner > "$CN_PROBE_OUT/shell.log" 2>&1 &
nest_pid=$!
printf '%s\n' "$nest_pid" > "$nest_root/session.pid"

waited=0
while [ ! -f "$CN_PROBE_OUT/done" ] && [ "$waited" -lt "$run_timeout" ] && kill -0 "$nest_pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
done

if [ ! -f "$CN_PROBE_OUT/done" ]; then
    echo "probe did not finish (waited ${waited}s); see $CN_PROBE_OUT/shell.log" >&2
    exit 1
fi

cat "$CN_PROBE_OUT/results.txt"
run_ok=1
if grep -q '^FAIL' "$CN_PROBE_OUT/results.txt"; then
    run_ok=0
fi
if [ "$scenario" = signin ]; then
    required_checks=(
        signin-page-shown
        indicator-signed-out
        captcha-page
        code-page
        devices-page
        signed-in-main
        indicator-signed-in
    )
elif [ "$scenario" = edit ]; then
    required_checks=(
        edit-mode-on
        no-go-drawn
        delete-after-draw
        zone-moved
        zone-resized
        delete-after-click
        select-drag-pans
        wall-drawn
        saved-and-shown
        merge-done
        return-keeps-editing
        discard-leaves
    )
elif [ "$scenario" = qr ]; then
    required_checks=(
        signin-page-shown
        qr-button-shown
        qr-page
        qr-approved-devices
        qr-signed-in
    )
else
    required_checks=(
        app-window-opens
        rooms-on-map
        click-room-selects
        clean-label
        list-check-selects
        click-again-deselects
        scroll-zooms
        double-click-fits
        clean-starts
        live-robot-moves
        indicator-in-panel
        indicator-follows-app
        close-keeps-running
        open-from-menu
        narrow-collapses
    )
fi
for check_name in "${required_checks[@]}"; do
    if ! grep -q -e "^PASS $check_name\$" -e "^FAIL $check_name:" "$CN_PROBE_OUT/results.txt"; then
        echo "missing check: $check_name" >&2
        run_ok=0
    fi
done

shell_log="$CN_PROBE_OUT/shell.log"
log_bad=0
# console.error / console.warn from the extension ("[chippy] ...") are
# logged by GJS as CRITICAL / WARNING.
if grep -aE '(CRITICAL|WARNING) .*\[chippy\]' "$shell_log" >&2; then log_bad=1; fi
if grep -a -A15 'JS ERROR' "$shell_log" | grep -a "$ext_uuid" >/dev/null; then log_bad=1; fi
if grep -a -A15 'Unhandled promise rejection' "$shell_log" | grep -a "$ext_uuid" >/dev/null; then log_bad=1; fi
if [ "$log_bad" = 1 ]; then
    echo "chippy extension error in $shell_log" >&2
    run_ok=0
fi
if grep -a 'Traceback' "$CN_PROBE_OUT/app.log" >&2; then
    echo "app traceback in $CN_PROBE_OUT/app.log" >&2
    run_ok=0
fi
# GLib criticals/warnings from the app (e.g. "Gtk-CRITICAL **"), except the nest-only
# accessibility bus noise (no org.a11y.atspi registry runs in the headless shell).
if grep -aE -- '-(CRITICAL|WARNING) \*\*' "$CN_PROBE_OUT/app.log" | grep -av 'org\.a11y\.atspi' >&2; then
    echo "app GLib critical/warning in $CN_PROBE_OUT/app.log" >&2
    run_ok=0
fi
echo "screenshots: $CN_PROBE_OUT"
[ "$run_ok" = 1 ]
