# Chippy — Design

Date: 2026-11-09. Status: approved in chat, pending spec review.

## Goal

A standalone GTK4/libadwaita app, **Chippy** (`io.github.eboye.Chippy`), for the user's Dreame
r2209 robot vacuum ("Xiaomi Robot Vacuum X10"). It signs in to Xiaomi itself, talks to the robot
directly, shows the room map from the cloud, cleans selected rooms, sends notifications, and can
install its own GNOME Shell top-bar extension. It replaces the `dreamed` helper from
`~/GitHub/dreame-vacuum-gnome`; that repo's working code is the starting point.

Approved look: the mockup from 2026-11-09 (map on the left, sidebar with status, room list,
suction, and a "Clean N rooms · area" button on the right).

## Facts carried over (verified live 2026-11-06/09)

- Robot `dreame.vacuum.r2209` on the LAN (fixed IP, e.g. 192.0.2.10), miIO UDP 54321, token from the Xiaomi cloud
  (numeric did such as 1000000001, region `de`). MIoT mapping as in `dreamed/mapping.py` (status 2.1, battery
  3.1, suction 4.4, water 4.5, tank 4.6, room clean action 4.1 with piid 1=18 and piid 10 selects).
- Map object name comes from property 6.8; the cloud file is a saved-maps JSON (`mapstr`,
  `curr_id`) whose entry is a base64+zlib frame: 27-byte header `<hhBhhhhhhhhhhh`
  (map id, frame id, type, robot x/y/angle, charger x/y/angle, grid mm, width, height, left,
  top), then width×height pixels, then a JSON trailer (`seg_inf`, with names and `nei_id`).
- Pixel: `p & 0x3f` = segment id (0 = outside), `p & 0x80` = edge. The image is stored bottom-up
  (flip vertically to match the phone app). Grid is 50 mm, so one pixel is 0.0025 m².
- Live map: 10 segments; 6 is a 12-pixel sliver. Charger at (500, 35) mm.
- Xiaomi login: password + captcha + email code flow already implemented in `dreamed/cloud.py`;
  the saved session (user_id, service_token, ssecurity) avoids repeat 2FA.
- The current keyring item has attribute `application=dreamed` and holds `Credentials` JSON
  (ip, token, did, model, country, username, password, cloud_session).
- Manual room names live in `~/.config/dreamed/room-names.json` (1 Bathroom, 2 Kitchen,
  3 Toilet, 4 Living room, 5 Storage, 7 Studio, 8 Bedroom, 9 Guest room, 10 Hallway).
- Machine: GTK 4.24, libadwaita 1.10, PyGObject 3.58 on system Python 3.14, Flatpak runtime
  `org.gnome.Platform//51` installed (`org.gnome.Sdk//51` not yet).

## Scope

In:
- Sign-in inside the app (email/ID + password, captcha image, email code), account page with
  sign out. **No re-login on first run**: the existing `dreamed` keyring item is imported.
- Map view: rooms in colors with name + area labels, charger marker, click to select, scroll /
  pinch to zoom, drag to pan, fit-to-window.
- Sidebar: status + battery + last clean, room list synced with the map, select all, suction,
  mopping level (only with a water tank), Clean N rooms, Start/Pause, Stop, Dock, Locate.
- Room management: rename a room, hide a room (hidden rooms leave the list and the labels; the
  sliver below 0.25 m² is hidden by default). Imports `~/.config/dreamed/room-names.json` once.
- Refresh map button (re-downloads from the cloud).
- Live position (added 2026-11-09, format confirmed on the r2209 2026-11-10): while cleaning or
  returning, every 5 s read the robot's recent MIoT property 6.1 values from the Xiaomi cloud
  (`/user/get_user_device_data`, type `prop`, key `6.1`). The r2209 answers the map request
  (action 6.1) with an empty object name; instead it uploads a small partial ("P") map frame
  about every 5 s, stored as a JSON list of url-safe base64 zlib frames. Draw the robot from the
  newest frame's header (x, y in map mm, heading in degrees, 0 = +x, counter-clockwise) and the
  path from the frames' trailer `tr` joined in order (`S`/`M`/`l` absolute points, `L` relative
  steps). Frames more than 2 minutes apart belong to different cleans. If nothing recent is
  stored, keep the saved map and show no position (back off to 30 s after 3 empty answers).
- Notifications (finished, error, needs attention, offline during a clean) as in `dreamed/notify.py`,
  sent as `Gio.Notification` so they belong to the app.
- Background mode: closing the window keeps Chippy running (polling, notifications, D-Bus for the
  extension); autostart at login via the Background portal. A preference turns this off.
- Top-bar extension bundled in the app: a "Show in top bar" switch installs/updates/removes it.
- Flatpak build (local and GitHub releases) and a run-from-source dev mode.

Out (later): zones / no-go areas, multiple robots,
Flathub submission (it would reject the extension installer), editing the map, encrypted maps
of newer Dreame models, profiles for models nobody has tested, non-Dreame brands.

## Architecture

One process, one GLib main loop.

```
Adw.Application io.github.eboye.Chippy
 ├─ core (no GTK; ported from dreamed with its tests)
 │    profiles · device (Vacuum) · cloud (XiaomiCloud) · mapdecode · credentials · notify · fake
 ├─ store      credentials import/save, room names + hidden set, cached map frame
 ├─ mapmodel   MapFrame → rooms (areas, bbox, label anchor, colour), hit-test, charger point
 ├─ controller VacuumController (GObject): poll timer, snapshot properties, actions on a worker
 │             thread, map/rooms refresh, notifier hookup; the only owner of the Vacuum
 ├─ dbus       exports io.github.eboye.Chippy.Vacuum at /io/github/eboye/Chippy on the app's
 │             own bus name (registered in do_dbus_register) for the extension
 ├─ ui         MainWindow (Adw.NavigationSplitView: map content + sidebar), MapView widget,
 │             LoginDialog, PreferencesDialog (account, background, top bar, rooms)
 └─ shellext   install / update / enable / disable / remove the bundled extension
extension/chippy@eboye.github.io   (GNOME 51 ESM, adapted from dreame-vacuum@)
```

- **Model profiles:** everything model-specific lives in `core/profiles.py`, nowhere else. A
  `Profile` holds: the model ids it covers, the MIoT property table (status, fault, battery,
  charging, area, time, suction, water, tank, map-object property), the action table (start,
  pause, stop, dock, locate, room clean, map request), the status/suction/water/fault value
  maps, the room-clean payload builder, and the map format (`plain` = base64+zlib frame,
  `encrypted` = unsupported for now). `device.Vacuum` and `mapping` take a profile instead of
  module constants. One profile ships: `r2209` (verified). Lookup: exact model id →
  that profile; any other `dreame.vacuum.*` → the r2209 profile marked `tested=False`;
  anything else → not supported.
- **Untested models:** sign-in accepts every Dreame vacuum on the account. For an untested model
  the account page and a dismissible banner say "Chippy hasn't been tested with <model>; some
  controls may not work". A map that doesn't decode as `plain` shows "This model's map can't be
  shown yet" in the map area; the room list (from cached/manual names) and controls still work.
  Non-Dreame vacuums are listed as "Not supported" and can't be picked.
- **Threads:** every robot or cloud call runs on a worker thread (`Gio.Task`-style helper or a
  `ThreadPoolExecutor`) and results return with `GLib.idle_add`. `Vacuum` keeps its lock. The
  UI never blocks.
- **Poller:** GLib timeouts: 15 s idle, 5 s while cleaning or returning, 60 s while offline;
  offline after 3 failures (same rules as `dreamed/poller.py`). An action pokes an immediate poll.
- **Map frame:** `mapdecode` gains `decode_map()` returning `MapFrame(width, height, grid_mm,
  left, top, charger, pixels, seg_inf)`; `decode_rooms()` stays as a thin wrapper. The last good
  payload is cached in the app's cache dir so the map shows offline and at startup.
- **Map widget:** a `Gtk.Widget` subclass with `do_snapshot`. Rooms are pre-rendered once per
  frame into a `Gdk.MemoryTexture` (one pixel per cell, colours per room, dimmed when not
  selected) and drawn scaled with nearest-neighbour filtering; labels, check badges and the
  charger are drawn on top with Pango/GSK. Gestures: click (select), scroll + zoom gesture,
  drag (pan); double-click resets the view. Keyboard: the room list is the accessible path.
- **D-Bus for the extension:** same members as today's `io.github.eboye.DreameVacuum` interface
  (State, Battery, Charging, Error, Suction, Water, HasWaterTank, Rooms a(us), CleanedArea,
  CleaningTime; Start, Pause, Stop, Dock, Locate, CleanRooms(au), SetSuction, SetWater,
  RefreshRooms) plus `ShowWindow`. Rooms exclude hidden rooms and carry the user's names.
- **Process lifetime:** `hold()` while background mode is on, so closing the window only hides
  it. `chippy --background` starts without a window (used by autostart).

## Secrets and data

- Keyring through `secretstorage` (direct Secret Service over D-Bus, not the libsecret portal,
  so the existing login-keyring item is visible inside Flatpak). Chippy's item:
  `application=chippy`, label "Chippy robot vacuum", same JSON shape as `Credentials`.
- First run: if no `chippy` item exists and a `dreamed` item does, copy it to a `chippy` item
  (the `dreamed` item is left alone until `dreamed` is retired).
- Sign out deletes the `chippy` item and the cached map.
- Non-secret data under the app's own XDG dirs: `config/rooms.json` (names, hidden ids),
  `config/settings.json` (background mode, autostart, top bar);
  `cache/map.json` (last cloud payload + room cache).
- Passwords never in logs; micloud's chatty logging filtered as in `dreamed/cli.py`.

## Sign-in flow (UI)

`LoginDialog` (Adw.Dialog with a navigation view):
1. Email/ID + password → background login.
2. If Xiaomi asks for a captcha: page with the captcha image (`Gtk.Picture` from bytes) and an
   entry; retry (up to 3).
3. If Xiaomi asks for 2FA: "We sent a code to your email" page, code entry, resend; 3 tries.
4. Device discovery across regions; if several vacuums are found, pick one from a list; robot IP
   from the cloud (`localip`), editable in the account page.
5. Save credentials, start polling, fetch the map.

Errors surface as an inline banner on the current page with Xiaomi's reason text.

## Top-bar extension

- Source lives in this repo under `extension/chippy@eboye.github.io` and is shipped as app data.
- Switch on: copy the folder to `~/.local/share/gnome-shell/extensions/chippy@eboye.github.io`
  (a real copy, never a symlink), then `org.gnome.Shell.Extensions.EnableExtension`. If Shell
  does not know it yet (new install), tell the user to log out and back in once.
- On app start with the switch on: if the installed `metadata.json` version is older, recopy.
- Switch off: `DisableExtension`, then delete that folder (only that folder, after checking it is
  a plain directory that Chippy wrote, i.e. contains a `.chippy-installed` marker).
- The extension: menu as today (status, battery, rooms submenu, suction, controls) plus
  "Open Chippy" (`ShowWindow`); talks to `io.github.eboye.Chippy`; no prefs page (names and
  hiding live in the app). When Chippy isn't running it shows "Chippy isn't running" with an
  "Open Chippy" item; that item calls `ShowWindow` with D-Bus auto-start, which launches the app (its desktop file is D-Bus activatable).
- Flatpak permissions this needs: `--filesystem=xdg-data/gnome-shell/extensions:create`,
  `--talk-name=org.gnome.Shell.Extensions`.

## Flatpak

Manifest `io.github.eboye.Chippy.json`, runtime `org.gnome.Platform//51` + `org.gnome.Sdk//51`,
Python deps (python-miio 0.5.12 and its deps, micloud, secretstorage, requests) vendored with
`flatpak-pip-generator`. finish-args: `--share=network`, `--socket=wayland`,
`--socket=fallback-x11`, `--share=ipc`, `--device=dri`, `--talk-name=org.freedesktop.secrets`,
`--filesystem=xdg-config/dreamed:ro` (one-time room-name import), the two extension
permissions above. Desktop file is `DBusActivatable=true`; metainfo + symbolic and full-colour
icons.

## Error handling

- Robot unreachable: "Chippy can't reach the robot" banner, controls insensitive, map still
  shown from cache; recovers on the next good poll.
- Cloud/map failure: toast "Map unavailable: <reason>", room list still from cache.
- Keyring locked and unlock dismissed: status page "Unlock your keyring" with a retry button.
- Not signed in: status page with "Sign in".
- Action failure (D-Bus or UI): toast with the robot's error text.

## Retiring dreamed (after Chippy is verified)

Ask the user first, then: disable and remove `dreamed.service`, `uv tool uninstall dreamed`,
disable the old `dreame-vacuum@eboye.github.io` extension and remove its **symlink** with
`rm` on the link (never a recursive delete through it), delete the `dreamed` keyring item, and
archive `dreame-vacuum-gnome` (README pointer to Chippy).

## Testing

- **Unit (pytest, no display):** ported `dreamed` tests for mapping, device (through the r2209 profile), profile lookup (exact, untested Dreame, unsupported), cloud (2FA,
  captcha), mapdecode, credentials, notify; new tests for `decode_map`, mapmodel (areas,
  anchors inside U-shaped rooms, hit-test with zoom/pan, hidden rooms), credentials import from
  the `dreamed` item, room store, poller timing (GLib loop with fake clock), D-Bus interface
  against a fake vacuum, shellext install/remove (temp dirs, marker check, refuses symlinks).
- **Real UI (required):** run Chippy with `--fake` (fake robot, fixture map shaped like the real
  one) inside the nested headless gnome-shell harness from `dreame-vacuum-gnome`, drive it with
  a real virtual pointer/keyboard (click rooms, check list sync, Clean button text, zoom, sign-in
  dialog pages with a fake cloud), take screenshots at two window sizes, light and dark.
- **Extension:** nested-shell run with Chippy `--fake` exporting the bus name.
- **Live (user present):** start the real app, check status/map/names match the robot without a
  new login, Locate. Room cleaning and suction changes only when the user asks.

## Constraints

- Never start a real clean without the user asking. Secrets only in the keyring. Repo-local git
  identity `eboye <624357+eboye@users.noreply.github.com>`; nothing pushed without asking. No
  heredocs. Nested-shell runs export XDG_*/DCONF_PROFILE before `dbus-run-session`.
