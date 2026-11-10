# Map editor

Date: 2026-11-10. Status: approved (design discussed in chat: edit mode with Save, room panel in
edit mode, explicit "Use room settings" switch).

## Goal

Edit the robot's saved map from Chippy: no-go zones, no-mop zones, virtual walls, room names on
the robot, per-room settings (suction, mopping, passes), cleaning order, merge and split rooms.
Everything model-specific stays in `core/profiles.py`; r2209 support for each command is
unverified until the live check.

## Robot protocol (from Tasshack/dreame-vacuum, `device.py`/`map.py`; verified shapes on the r2209)

- **Update map:** action `6.2` (`map_update`) with one in-parameter `piid 4`
  (`MAP_EXTEND_DATA`) whose value is a compact JSON string (`separators=(",", ":")`). One key per
  call:
  - zones and walls (always the complete set):
    `{"vw": {"line": [[x1,y1,x2,y2],…], "rect": [[x1,y1,x2,y2],…], "mop": [[x1,y1,x2,y2],…]}}`,
    map mm, rectangles axis-aligned with `x1<x2`, `y1<y2`.
  - room name: `{"nsr": {"<seg>": {"name": base64(utf-8 name), "type": 0, "index": 0}},
    "mapid": <map id>}`.
  - per-room settings: `{"customeClean": [[seg, suction, water, passes], …]}` for every room;
    suction 0–3 (quiet…turbo), water 1–3 (low…high, the profile values), passes 1–3.
  - cleaning order: `{"cleanOrder": [seg, …]}` (every room, in order).
  - merge: `{"msr": [seg_a, seg_b], "mapid": <map id>}`.
  - split: `{"dsrid": [x1, y1, x2, y2, seg], "mapid": <map id>}` (the line in map mm).
- **Customized cleaning switch:** property `4.26` (0 off, 1 on); read with the status poll.
- **Map id:** the saved-maps document's current map (`curr_id`).
- **Reading current state:**
  - zones/walls: the saved map trailer's `vw` (`line`/`rect`/`mop`, same shape as above; the
    r2209 sends all three, empty today);
  - per-room settings and order: `cleanset` in the robot's live frames (property 6.1 history):
    `{"<seg>": [suction, water(1–3), passes, order, …]}` (e.g. `[1, 3, 1, 2, -1]`). Chippy reads
    the newest `cleanset` from the last 7 days of 6.1 history when edit mode opens; without one,
    every room shows the defaults (standard, medium, 1 pass, order = room id order) and the
    panel says "Settings from the robot aren't known yet; saving sets them."

## Design

### Core (GTK-free)
- `core/mapdecode.py`: `MapFrame` gains `map_id` (header field 0), `zones: MapZones` (from
  `vw`). `decode_saved_map_id(payload)` returns the saved-maps document's `curr_id` (or the
  header map id for a bare frame).
- `core/mapedit.py` (new):
  - `Rect(x1, y1, x2, y2)` normalised; `Line(x1, y1, x2, y2)`;
    `MapZones(no_go: tuple[Rect], no_mop: tuple[Rect], walls: tuple[Line])`.
  - `RoomSetting(suction: str, water: str, passes: int)`; `parse_cleanset(obj, profile) ->
    (dict[int, RoomSetting], list[int] order)`.
  - `EditSession(zones, names, settings, order, use_room_settings)`: the staged copy;
    `changes(original) -> list[MapEdit]` in a fixed order (zones, names, settings, order,
    switch), each `MapEdit(kind, payload)` with the exact JSON above (or the property write).
  - `merge_payload(map_id, a, b)`, `split_payload(map_id, seg, line)`.
  - `remap_names(before: dict[int, str], rooms_before, rooms_after, kind)`: after a merge the
    surviving id keeps its name (the other is dropped); after a split the new id gets
    "<name> 2".
- `core/device.py`: `Vacuum.update_map(payload: dict)` (action 6.2, piid 4, compact JSON;
  raises `DeviceError` on a non-zero code or a result with `code != 0` in its `out`),
  `Vacuum.set_room_settings_enabled(on: bool)` (4.26). `Snapshot.room_settings: bool | None`.
- `core/profiles.py`: R2209 `actions["map_update"] = (6, 2)`, `map_update_piid = 4`,
  `props["room_settings"] = (4, 26)`, `edit_support` = all kinds (an unsupported kind is
  recorded at runtime, see below).
- `core/fake.py`: `FakeVacuum.update_map` records payloads and applies them to its fake map
  (zones into `vw`, names into `seg_inf`, merge/split renumber the fake layout, settings/order
  into a fake cleanset); `build_fake_payload` emits `vw`; fake live frames carry `cleanset`.

### Controller
- `VacuumController.can_edit` (False while cleaning/returning/offline/expired).
- `begin_edit(on_done(session))`: fetches the newest cleanset (cloud runner) and builds an
  `EditSession` from the current map, names (robot names; Chippy's local renames are shown and
  saved to the robot), settings, order and switch.
- `save_edit(session, on_done(error))`: sends `session.changes(...)` one by one on the device
  runner; stops at the first failure (error names the step), then always re-downloads the map.
  A `DeviceError` for a kind marks it unsupported for this session ("Not supported on this
  model") and disables its control.
- `merge_rooms(a, b, on_done)` / `split_room(seg, line, on_done)`: immediate, then map refresh
  and `RoomStore` remap (names keyed by new ids).
- Signals: `edit-saved`, existing `map-changed`.

### UI
- Header: "Edit map" toggle (pencil icon), insensitive unless `can_edit`.
- Outside edit mode the map draws zones (no-go: red hatched, no-mop: blue hatched) and walls
  (red lines). Order badges on room labels show in edit mode only (amended 2026-11-10: outside
  edit mode the order would need a cloud fetch on every map load and mostly adds clutter).
- Edit mode:
  - Map toolbar (overlay, top-left): Select, No-go zone, No-mop zone, Wall, Order, Split, Merge.
  - Select: click a zone/wall to select (handles: rect corners, line ends; drag to move/resize;
    Delete key or trash button removes); click a room to open its panel.
  - No-go/No-mop: drag a rectangle (min 0.3 m each side). Wall: drag a line (min 0.3 m).
  - Order: click rooms in sequence; badges show 1…n; "Reset order" in the panel.
  - Split: click a room, drag a line across it; confirm dialog "Split <room>?" → immediate.
  - Merge: click two neighbouring rooms (`nei_id`); confirm "Merge <a> and <b>?" → immediate.
  - Sidebar becomes the edit panel: "Use room settings" switch; room panel for the selected
    room (name entry, Suction, Mopping, Passes 1–3, order position read-only); bottom bar
    "Cancel" and "Save to robot" (suggested, insensitive when nothing changed). Leaving edit
    mode with changes asks "Discard changes?".
  - While a save runs: everything insensitive, spinner on Save; success toast "Saved to robot";
    failure toast with the message, edits kept.
- When "Use room settings" is on, the sidebar's Suction/Mopping rows show "Per room" and room
  cleaning sends no level changes.

## Testing
- Unit: zone parsing (r2209 empty `vw`, filled, junk), payload building (exact JSON strings),
  `changes()` only for what changed and in order, cleanset parsing, name remap, controller
  save sequence with a failing step, merge/split refresh + remap, `can_edit`.
- Nested check: `--scenario edit`: enter edit mode, draw a no-go zone and a wall, rename a room,
  save, check the fake robot received the payloads and the map redraws them; merge two rooms
  and split one on the fake.
- Live (user present), each reversible first: add a no-go zone → check in the phone app →
  remove; rename a room → rename back; change one room's passes → back; order → back; switch
  on → off. Merge/split only with the user's go-ahead at that moment.
