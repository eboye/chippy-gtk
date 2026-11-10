# Map Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edit the robot's saved map from Chippy — no-go/no-mop zones, virtual walls, room names on the robot, per-room settings, cleaning order, merge/split — through an edit mode with Save/Cancel.

**Architecture:** A GTK-free `core/mapedit.py` owns the data (zones, room settings, order), the staged `EditSession` and the exact robot payloads; `Vacuum.update_map` sends them (action 6.2). The controller orchestrates saves, immediate merge/split, map refresh and local-name remapping. `MapView` gains read-only overlays and edit tools; the sidebar swaps to an edit panel.

**Tech Stack:** Python 3.14, PyGObject (GTK 4.24, libadwaita 1.10, Gsk paths), python-miio 0.5.12, pytest, the nested GNOME Shell check (`tools/nested-check.sh` + probe extension).

**Spec:** `docs/superpowers/specs/2026-11-10-map-editor-design.md`

## Global Constraints

- Branch `feat/map-editor` (based on `feat/qr-signin`). Line length 100; ruff E F I UP B; no heredocs.
- Everything model-specific (siid/piid/aiid, value ranges) lives in `core/profiles.py`.
- Never send anything to the real robot in tests or the nested check; fake robot only.
- Robot payloads are compact JSON (`json.dumps(obj, separators=(",", ":"))`), map coordinates in integer mm.
- Rectangles normalised `x1 < x2`, `y1 < y2`; minimum zone/wall size 300 mm.
- Suction names `quiet|standard|strong|turbo` ↔ 0–3; water names `low|medium|high` ↔ 1–3 (the profile values, the same numbers the robot uses in `cleanset`; no offset); passes 1–3.
- UI copy: "Edit map", "Save to robot", "Cancel", "Use room settings", "Per room", "Discard changes?", "Split {room}?", "Merge {a} and {b}?", "Saved to robot", "Not supported on this model", "Settings from the robot aren't known yet; saving sets them.", tools "Select", "No-go zone", "No-mop zone", "Wall", "Order", "Split", "Merge".
- Every UI change is verified in the nested check with real (virtual) input and screenshots.

---

### Task 1: Zones, room settings and payloads (core/mapedit.py, mapdecode)

**Files:**
- Create: `src/chippy/core/mapedit.py`
- Modify: `src/chippy/core/mapdecode.py` (`MapFrame.map_id`, `MapFrame.zones`, saved-map id)
- Test: `tests/test_mapedit.py` (new), `tests/test_mapdecode.py`

**Interfaces:**
- Produces:
  - `Rect(x1: int, y1: int, x2: int, y2: int)` frozen; `Rect.of(a, b, c, d) -> Rect` (normalises); `Line(x1, y1, x2, y2)` frozen.
  - `MapZones(no_go: tuple[Rect, ...] = (), no_mop: tuple[Rect, ...] = (), walls: tuple[Line, ...] = ())` frozen; `MapZones.parse(vw: object) -> MapZones` (junk-tolerant); `MapZones.payload() -> dict` → `{"vw": {"line": [...], "rect": [...], "mop": [...]}}`.
  - `RoomSetting(suction: str, water: str, passes: int)` frozen; `DEFAULT_SETTING = RoomSetting("standard", "medium", 1)`.
  - `parse_cleanset(obj, profile) -> tuple[dict[int, RoomSetting], list[int]] | None` (None when `obj` isn't a usable cleanset).
  - `EditSession` dataclass: `map_id: int`, `zones: MapZones`, `names: dict[int, str]`, `settings: dict[int, RoomSetting]`, `order: list[int]`, `use_room_settings: bool`, `settings_known: bool`; `copy() -> EditSession`; `changes(original: EditSession, profile) -> list[MapEdit]`.
  - `MapEdit(kind: str, payload: dict | None, value: bool | None = None)` frozen; kinds in this order: `"zones"`, `"names"`, `"settings"`, `"order"`, `"room_settings"` (the 4.26 switch; `payload=None`, `value=True/False`).
  - `merge_payload(map_id, a, b) -> dict`, `split_payload(map_id, seg, line: Line) -> dict`.
  - `remap_after_merge(names: dict[int, str], kept: int, removed: int) -> dict[int, str]`; `remap_after_split(names, seg: int, new_ids: set[int]) -> dict[int, str]`.
  - `mapdecode.MapFrame` gains `map_id: int = 0` and `zones: MapZones = MapZones()`; `decode_map` fills them (`zones` from trailer `vw`); `saved_map_id(payload) -> int` returns the saved-maps doc `curr_id` (int) or the frame header map id.

- [ ] **Step 1: Write the failing tests** — `tests/test_mapedit.py`:

```python
import base64
import json

from chippy.core.mapedit import (
    DEFAULT_SETTING,
    EditSession,
    Line,
    MapEdit,
    MapZones,
    Rect,
    RoomSetting,
    merge_payload,
    parse_cleanset,
    remap_after_merge,
    remap_after_split,
    split_payload,
)
from chippy.core.profiles import R2209


def test_rect_normalises():
    assert Rect.of(500, 900, -100, 200) == Rect(-100, 200, 500, 900)


def test_zones_parse_r2209_empty_and_filled():
    assert MapZones.parse({"line": [], "rect": [], "mop": []}) == MapZones()
    vw = {"line": [[0, 0, 1000, 0]], "rect": [[300, 400, 100, 0]], "mop": [[1, 2, 3, 4]]}
    zones = MapZones.parse(vw)
    assert zones.walls == (Line(0, 0, 1000, 0),)
    assert zones.no_go == (Rect(100, 0, 300, 400),)
    assert zones.no_mop == (Rect(1, 2, 3, 4),)


def test_zones_parse_skips_junk():
    assert MapZones.parse(None) == MapZones()
    assert MapZones.parse({"rect": [[1, 2, 3]], "line": ["x"], "mop": 5}) == MapZones()


def test_zones_payload_is_the_complete_set():
    zones = MapZones(no_go=(Rect(0, 0, 500, 500),), walls=(Line(1, 2, 3, 4),))
    assert zones.payload() == {
        "vw": {"line": [[1, 2, 3, 4]], "rect": [[0, 0, 500, 500]], "mop": []}
    }


def test_parse_cleanset_from_a_live_frame():
    settings, order = parse_cleanset(
        {"1": [1, 3, 1, 2, -1], "2": [3, 1, 2, 1, -1], "x": [0]}, R2209
    )
    assert settings == {
        1: RoomSetting("standard", "high", 1),
        2: RoomSetting("turbo", "low", 2),
    }
    assert order == [2, 1]
    assert parse_cleanset("not a dict", R2209) is None
    assert parse_cleanset(json.dumps({"1": [0, 2, 1, 1]}), R2209)[0] == {
        1: RoomSetting("quiet", "medium", 1)
    }


def _session(**kw):
    base = dict(
        map_id=7,
        zones=MapZones(),
        names={1: "Kitchen", 2: "Bath"},
        settings={1: DEFAULT_SETTING, 2: DEFAULT_SETTING},
        order=[1, 2],
        use_room_settings=False,
        settings_known=True,
    )
    base.update(kw)
    return EditSession(**base)


def test_no_changes_no_edits():
    s = _session()
    assert s.changes(s.copy(), R2209) == []


def test_changes_in_fixed_order_with_exact_payloads():
    before = _session()
    after = before.copy()
    after.zones = MapZones(no_mop=(Rect(0, 0, 400, 400),))
    after.names[2] = "Bathroom"
    after.settings[1] = RoomSetting("turbo", "low", 2)
    after.order = [2, 1]
    after.use_room_settings = True
    edits = after.changes(before, R2209)
    assert [e.kind for e in edits] == ["zones", "names", "settings", "order", "room_settings"]
    assert edits[0].payload == {"vw": {"line": [], "rect": [], "mop": [[0, 0, 400, 400]]}}
    name = base64.b64encode("Bathroom".encode()).decode()
    assert edits[1].payload == {
        "nsr": {"2": {"name": name, "type": 0, "index": 0}},
        "mapid": 7,
    }
    assert edits[2].payload == {"customeClean": [[1, 3, 1, 2], [2, 1, 2, 1]]}
    assert edits[3].payload == {"cleanOrder": [2, 1]}
    assert edits[4] == MapEdit("room_settings", None, True)


def test_unknown_settings_are_sent_once_saved_even_if_unchanged():
    before = _session(settings_known=False)
    after = before.copy()
    after.use_room_settings = True
    kinds = [e.kind for e in after.changes(before, R2209)]
    assert kinds == ["settings", "room_settings"]


def test_merge_split_payloads():
    assert merge_payload(7, 3, 4) == {"msr": [3, 4], "mapid": 7}
    assert split_payload(7, 5, Line(0, 10, 900, 10)) == {"dsrid": [0, 10, 900, 10, 5], "mapid": 7}


def test_remap_names():
    names = {1: "Kitchen", 2: "Bath", 3: "Hall"}
    assert remap_after_merge(names, kept=2, removed=3) == {1: "Kitchen", 2: "Bath"}
    assert remap_after_split(names, seg=2, new_ids={11}) == {
        1: "Kitchen", 2: "Bath", 3: "Hall", 11: "Bath 2",
    }
```

and append to `tests/test_mapdecode.py`:

```python
def test_frame_carries_map_id_and_zones():
    header = struct.pack("<hhBhhhhhhhhhhh", 4, 1, 73, 0, 0, 0, 0, 0, 0, 50, 2, 2, 0, 0)
    vw = {"line": [], "rect": [[0, 0, 500, 500]], "mop": []}
    trailer = json.dumps({"seg_inf": {}, "vw": vw}).encode()
    frame = decode_map(zlib.compress(header + bytes(4) + trailer))
    assert frame.map_id == 4
    assert len(frame.zones.no_go) == 1


def test_saved_map_id_reads_curr_id():
    from chippy.core.mapdecode import saved_map_id
    from chippy.core.fake import build_fake_payload

    assert saved_map_id(build_fake_payload()) == 0
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/test_mapedit.py tests/test_mapdecode.py -q`
Expected: ImportError / AttributeError.

- [ ] **Step 3: Implement `src/chippy/core/mapedit.py`**:

```python
"""Map editing data: zones and walls, per-room settings, cleaning order, and the exact
payloads Dreame robots take on action 6.2 (see the spec for the protocol)."""

import base64
import json
from dataclasses import dataclass, field, replace

from .state import name_to_value

MIN_SIZE_MM = 300


def _ints(item, n: int) -> list[int] | None:
    if not isinstance(item, list | tuple) or len(item) != n:
        return None
    try:
        return [int(v) for v in item]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Rect:
    x1: int
    y1: int
    x2: int
    y2: int

    @classmethod
    def of(cls, a: int, b: int, c: int, d: int) -> "Rect":
        return cls(min(a, c), min(b, d), max(a, c), max(b, d))

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass(frozen=True)
class Line:
    x1: int
    y1: int
    x2: int
    y2: int

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass(frozen=True)
class MapZones:
    no_go: tuple[Rect, ...] = ()
    no_mop: tuple[Rect, ...] = ()
    walls: tuple[Line, ...] = ()

    @classmethod
    def parse(cls, vw: object) -> "MapZones":
        if not isinstance(vw, dict):
            return cls()

        def rects(key):
            items = vw.get(key) if isinstance(vw.get(key), list) else []
            return tuple(Rect.of(*v) for v in (_ints(i, 4) for i in items) if v)

        lines = vw.get("line") if isinstance(vw.get("line"), list) else []
        walls = tuple(Line(*v) for v in (_ints(i, 4) for i in lines) if v)
        return cls(rects("rect"), rects("mop"), walls)

    def payload(self) -> dict:
        return {
            "vw": {
                "line": [w.as_list() for w in self.walls],
                "rect": [r.as_list() for r in self.no_go],
                "mop": [r.as_list() for r in self.no_mop],
            }
        }


@dataclass(frozen=True)
class RoomSetting:
    suction: str
    water: str
    passes: int


DEFAULT_SETTING = RoomSetting("standard", "medium", 1)


def parse_cleanset(obj, profile) -> tuple[dict[int, RoomSetting], list[int]] | None:
    """A live frame's `cleanset`: {"<seg>": [suction, water 1-3, passes, order, ...]}."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return None
    if not isinstance(obj, dict) or not obj:
        return None
    settings: dict[int, RoomSetting] = {}
    order_of: dict[int, int] = {}
    for key, values in obj.items():
        if not (isinstance(key, str) and key.isdigit()) or not isinstance(values, list):
            continue
        if len(values) < 3:
            continue
        try:
            suction, water, passes = int(values[0]), int(values[1]), int(values[2])
        except (TypeError, ValueError):
            continue
        seg = int(key)
        settings[seg] = RoomSetting(
            profile.suction.get(suction, "standard"),
            profile.water.get(water, "medium"),
            min(max(passes, 1), 3),
        )
        if len(values) > 3 and isinstance(values[3], int) and values[3] > 0:
            order_of[seg] = values[3]
    if not settings:
        return None
    order = sorted(settings, key=lambda s: (order_of.get(s, 10_000), s))
    return settings, order


@dataclass(frozen=True)
class MapEdit:
    kind: str
    payload: dict | None
    value: bool | None = None


@dataclass
class EditSession:
    map_id: int
    zones: MapZones
    names: dict[int, str]
    settings: dict[int, RoomSetting]
    order: list[int]
    use_room_settings: bool
    settings_known: bool
    unsupported: set[str] = field(default_factory=set)

    def copy(self) -> "EditSession":
        return replace(
            self,
            names=dict(self.names),
            settings=dict(self.settings),
            order=list(self.order),
            unsupported=set(self.unsupported),
        )

    def changes(self, original: "EditSession", profile) -> list[MapEdit]:
        edits: list[MapEdit] = []
        if self.zones != original.zones:
            edits.append(MapEdit("zones", self.zones.payload()))
        renamed = {
            seg: name
            for seg, name in self.names.items()
            if name.strip() and name.strip() != original.names.get(seg)
        }
        if renamed:
            nsr = {
                str(seg): {
                    "name": base64.b64encode(name.strip().encode("utf-8")).decode(),
                    "type": 0,
                    "index": 0,
                }
                for seg, name in sorted(renamed.items())
            }
            edits.append(MapEdit("names", {"nsr": nsr, "mapid": self.map_id}))
        if self.settings != original.settings or (
            not original.settings_known and self.use_room_settings
        ):
            rows = [
                [
                    seg,
                    name_to_value(profile.suction, s.suction),
                    name_to_value(profile.water, s.water),
                    s.passes,
                ]
                for seg, s in sorted(self.settings.items())
            ]
            edits.append(MapEdit("settings", {"customeClean": rows}))
        if self.order != original.order:
            edits.append(MapEdit("order", {"cleanOrder": list(self.order)}))
        if self.use_room_settings != original.use_room_settings:
            edits.append(MapEdit("room_settings", None, self.use_room_settings))
        return edits


def merge_payload(map_id: int, a: int, b: int) -> dict:
    return {"msr": [a, b], "mapid": map_id}


def split_payload(map_id: int, seg: int, line: Line) -> dict:
    return {"dsrid": [*line.as_list(), seg], "mapid": map_id}


def remap_after_merge(names: dict[int, str], kept: int, removed: int) -> dict[int, str]:
    out = dict(names)
    out.pop(removed, None)
    return out


def remap_after_split(names: dict[int, str], seg: int, new_ids: set[int]) -> dict[int, str]:
    out = dict(names)
    base = names.get(seg)
    if base:
        for index, new in enumerate(sorted(new_ids), start=2):
            out.setdefault(new, f"{base} {index}")
    return out
```

Note: `profiles.R2209.water` is `{1: "low", 2: "medium", 3: "high"}`, the same numbers the robot uses in `cleanset`, so water maps 1:1 (no offset). Check against the real live frame value `[1, 3, 1, 2, -1]`: standard suction, high water, 1 pass, order 2. The profile mapping is the single source of truth.

In `src/chippy/core/mapdecode.py`: import `MapZones` from `.mapedit` (no cycle: mapedit does not import mapdecode), add `map_id: int = 0` and `zones: MapZones = field(default_factory=MapZones)` to `MapFrame`, fill `map_id=fields[0]`, `zones=MapZones.parse(info.get("vw"))` in `decode_map`, and add:

```python
def saved_map_id(payload: bytes | str) -> int:
    """The current saved map's id (`curr_id` of the saved-maps document), else the header's."""
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("curr_id"), int):
        return doc["curr_id"]
    return decode_map(payload).map_id
```

- [ ] **Step 4: Run the tests** — `.venv/bin/python -m pytest tests/test_mapedit.py tests/test_mapdecode.py -q` → all pass (fix the water mapping per the note and keep the tests consistent).
- [ ] **Step 5: Lint, full suite, commit** — `ruff format/check`, `pytest -q`; `git commit -m "Map editor core: zones, room settings, order, robot payloads"`.

---

### Task 2: Robot commands (profile + device)

**Files:**
- Modify: `src/chippy/core/profiles.py` (R2209: `actions["map_update"] = (6, 2)`, `map_update_piid: int = 4`, `props["room_settings"] = (4, 26)`; `Profile.map_update_piid: int | None = None` default)
- Modify: `src/chippy/core/state.py` (`Snapshot.room_settings: bool | None = None`), `Profile.parse_properties` fills it (`values.get("room_settings") == 1` when present, else None)
- Modify: `src/chippy/core/device.py` (`update_map`, `set_room_settings_enabled`)
- Test: `tests/test_device.py`, `tests/test_profiles.py`

**Interfaces:**
- Consumes: Task 1 payload dicts.
- Produces: `Vacuum.update_map(payload: dict) -> None` (raises `DeviceError("the robot refused the map change (code N)")` on a non-zero code — including a non-zero `code` inside `out` items); `Vacuum.set_room_settings_enabled(on: bool) -> None`; `Snapshot.room_settings`.

- [ ] **Step 1: Failing tests** — in `tests/test_device.py` (reuse its fake MiotLike recorder; read the file first and follow its pattern):

```python
def test_update_map_sends_compact_json_on_6_2(recorder):
    vac = Vacuum(recorder, R2209)
    vac.update_map({"vw": {"line": [], "rect": [[0, 0, 500, 500]], "mop": []}})
    assert recorder.actions[-1] == (
        6, 2, [{"piid": 4, "value": '{"vw":{"line":[],"rect":[[0,0,500,500]],"mop":[]}}'}]
    )


def test_update_map_refusal_is_a_device_error(recorder):
    recorder.action_result = {"code": -1}
    with pytest.raises(DeviceError, match="refused"):
        Vacuum(recorder, R2209).update_map({"cleanOrder": [1]})


def test_room_settings_switch_is_4_26(recorder):
    Vacuum(recorder, R2209).set_room_settings_enabled(True)
    assert recorder.sets[-1][:3] == (4, 26, 1)
```

(Adapt fixture names to the existing test helpers; if none exist, add a small `Recorder` class implementing `call_action_by`/`set_property_by`/`get_properties` that records calls and returns `{"code": 0}` / `[{"code": 0}]`.)

In `tests/test_profiles.py`: parse a result list containing `{"siid": 4, "piid": 26, "code": 0, "value": 1}` → `snap.room_settings is True`; without it → `None`.

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** in `device.py`:

```python
    def update_map(self, payload: dict) -> None:
        siid, aiid = self._profile.actions["map_update"]
        value = json.dumps(payload, separators=(",", ":"))
        result = self._action(siid, aiid, [{"piid": self._profile.map_update_piid, "value": value}])
        for item in result.get("out") or []:
            code = item.get("code", 0) if isinstance(item, dict) else 0
            if code:
                raise DeviceError(f"the robot refused the map change (code {code})")

    def set_room_settings_enabled(self, on: bool) -> None:
        self._set(*self._profile.props["room_settings"], 1 if on else 0)
```

and make `_action` raise `DeviceError(f"the robot refused the map change (code {code})")`-style messages only from `update_map` (wrap: catch `DeviceError` from `_action` in `update_map` and re-raise with "refused" wording). Add `"room_settings": (4, 26)` to R2209 props, `"map_update": (6, 2)` to actions, `map_update_piid=4`.

- [ ] **Step 4: Run tests → pass. Step 5: lint, suite, commit** `"Robot: update-map command and room-settings switch"`.

---

### Task 3: Fake robot that edits its map

**Files:**
- Modify: `src/chippy/core/fake.py`
- Test: `tests/test_fake_map.py`, `tests/test_fake.py`

**Interfaces:**
- Consumes: Task 1 (`MapZones`, payload shapes), Task 2 (`update_map`, `set_room_settings_enabled`, `Snapshot.room_settings`).
- Produces: `FakeMap` (module-level mutable state object used by both `FakeVacuum` and `build_fake_payload`): `layout: list[str]`, `names: dict[int, str]`, `zones: MapZones`, `cleanset: dict[str, list[int]]`, `room_settings: bool`; `FakeVacuum(profile, fault=0, fake_map: FakeMap | None = None)`; `FakeVacuum.update_map(payload)`, `FakeVacuum.set_room_settings_enabled(on)`, `FakeVacuum.edits: list[dict]` (every payload received); `build_fake_payload(robot=None, path=None, fake_map: FakeMap | None = None)` emits `vw` from `fake_map.zones` and names from `fake_map.names`; `fake_live_payload(step, fake_map=None)` frames carry `cleanset`.
  - `update_map` semantics: `vw` → replace zones; `nsr` → set names (base64-decode); `customeClean` → cleanset rows `[suction, water, passes, order_kept, -1]`; `cleanOrder` → order element; `msr [a, b]` → every `b` cell becomes `a`, `b` removed from names/cleanset; `dsrid [x1,y1,x2,y2,seg]` → cells of `seg` on the right/lower side of the line become a new id `max(ids)+1` (use the sign of the cross product of the line direction and the cell centre in mm), name `"<name> 2"` is NOT set (the robot doesn't name new rooms; Chippy's remap does), cleanset gets a default row. Unknown keys → `DeviceError("unsupported")`.
  - A fresh `FakeMap()` equals today's fake (same layout, names, empty zones, cleanset `{"1": [1, 2, 1, 1, -1], ...}` for ids 1–5, `room_settings=False`).

- [ ] **Step 1: Failing tests** (append to `tests/test_fake_map.py`):

```python
from chippy.core.fake import FakeMap, FakeVacuum, build_fake_payload
from chippy.core.mapdecode import decode_map
from chippy.core.mapedit import MapZones, Rect
from chippy.mapmodel import grid_from_frame, room_areas


def test_fake_robot_applies_zones_and_names():
    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    vac.update_map(MapZones(no_go=(Rect(0, 0, 600, 600),)).payload())
    vac.update_map({"nsr": {"2": {"name": "S2l0Y2hlbiAy", "type": 0, "index": 0}}, "mapid": 0})
    frame = decode_map(build_fake_payload(fake_map=fm))
    assert frame.zones.no_go == (Rect(0, 0, 600, 600),)
    assert frame.names[2] == "Kitchen 2"


def test_fake_robot_merges_and_splits():
    fm = FakeMap()
    vac = FakeVacuum(fake_map=fm)
    vac.update_map({"msr": [1, 2], "mapid": 0})
    areas = room_areas(grid_from_frame(decode_map(build_fake_payload(fake_map=fm))))
    assert 2 not in areas and areas[1] > 0
    before = set(areas)
    # a vertical line through the middle of room 3 (bedroom)
    vac.update_map({"dsrid": [-600, -1000, -600, 1400, 3], "mapid": 0})
    after = set(room_areas(grid_from_frame(decode_map(build_fake_payload(fake_map=fm)))))
    assert len(after - before) == 1
```

(Choose the split line coordinates from `_cell_mm` so it crosses room 3; compute them in the test with `fake._cell_mm(20, 12)` etc. rather than hard-coding if clearer.)

- [ ] **Step 2: Run → fail. Step 3: Implement** `FakeMap` + the semantics above; keep `build_fake_payload()` with no args producing exactly today's bytes plus `"vw": {"line": [], "rect": [], "mop": []}` (update the existing fake-map tests only where they compare the trailer). `FakeVacuum.poll()` sets `room_settings` from the fake map. App: `_start_controller` fake branch builds one `FakeMap()` and passes it to `FakeVacuum`, `build_fake_payload` (via `functools.partial`) and `fake_live_payload`.
- [ ] **Step 4: Tests pass. Step 5: lint, suite, commit** `"Fake robot: editable map (zones, names, settings, order, merge, split)"`.

---

### Task 4: Reading the robot's room settings

**Files:**
- Modify: `src/chippy/core/live.py` (`latest_cleanset(history, now) -> object | None`)
- Modify: `src/chippy/app.py` (controller gets `fetch_cleanset`)
- Modify: `src/chippy/controller.py` (constructor arg `fetch_cleanset: Callable[[], object | None] | None = None`)
- Test: `tests/test_live.py`

**Interfaces:**
- Produces: `latest_cleanset(history: History, now: int, days: int = 7) -> object | None` — reads `history(now - days*86400, now + 60)` and returns the `cleanset` of the newest decodable frame that has one; `CLEANSET_LOOKBACK_DAYS = 7`.
- Real app: `fetch_cleanset = lambda: latest_cleanset(lambda s, e: cloud_map.property_history(creds.did, LIVE_PROPERTY, s, e, HISTORY_LIMIT), int(time.time()))`. Fake: `lambda: fake_map.cleanset`.
- `MapFrame` does not carry `cleanset`; `latest_cleanset` decodes it from the trailer itself (`json.loads` of the bytes after the pixels, like `decode_map`) — add a small `frame_trailer(value) -> dict` helper in `mapdecode.py` returning the trailer dict (or `{}`) and reuse it in `decode_map`.

- [ ] **Step 1: Failing test** (append to `tests/test_live.py`): build two frames with `_frame` extended by a `cleanset=` argument (add a `trailer` dict parameter to the existing `_frame` helper), rows at t=100 (cleanset A) and t=200 (cleanset B) and t=300 (no cleanset); `latest_cleanset(history, 400)` returns B; with no rows → None.
- [ ] **Step 2–4:** implement, pass.
- [ ] **Step 5: commit** `"Live: read the newest room settings (cleanset) from the robot's history"`.

---

### Task 5: Controller editing API

**Files:**
- Modify: `src/chippy/controller.py`, `src/chippy/store.py` (`RoomStore.replace_names(names: dict[int, str])`)
- Test: `tests/test_controller.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces (all callbacks on the GLib loop, always called exactly once, `RuntimeError("Chippy has stopped")` after `stop()`):
  - `can_edit -> bool` property: map loaded, not `cloud_expired`, state not in `cleaning|returning|paused|offline`.
  - `begin_edit(on_done: Callable[[EditSession | None, Exception | None], None])` — on the cloud runner calls `fetch_cleanset()`; builds the session: `map_id=self._map_id` (from `saved_map_id(payload)` stored in `_on_map`), `zones=self._frame.zones`, `names={r.id: r.name for r in self.rooms if not r.hidden}`, settings/order from `parse_cleanset` (or defaults for every visible room, `settings_known=False`, order = sorted ids), `use_room_settings=bool(self.snapshot.room_settings)`.
  - `save_edit(original: EditSession, edited: EditSession, on_done(error: Exception | None, failed_kind: str | None))` — runs `edited.changes(original, profile)` sequentially on the device runner: `zones/names/settings/order` → `vacuum.update_map(payload)`; `room_settings` → `vacuum.set_room_settings_enabled(value)`. Stops at the first failure. After a successful `names` edit, removes Chippy's local names for those rooms (`RoomStore.rename(seg, "")`) so the robot's names show. Then `refresh_map()` and `poke()` regardless of outcome, then `on_done`.
  - `merge_rooms(map_id, a, b, on_done(error))` / `split_room(map_id, seg, line, on_done(error))` — `update_map(merge_payload/split_payload)`, then refresh the map; when the new frame arrives, remap local names (`remap_after_merge` / `remap_after_split` with `new_ids = new ids − old ids`) via `RoomStore.replace_names`, then `on_done`.
  - Signal `"edit-saved"` (no args) after a fully successful save.

- [ ] **Step 1: Failing tests** with the fake robot + `InlineRunner` + `ManualScheduler` (see existing helpers `make(parts, vacuum=..., fetch=...)`): `can_edit` false while cleaning; `begin_edit` builds defaults when `fetch_cleanset` returns None; `save_edit` sends zones then name (check `vacuum.edits`), clears the local name, and the refreshed map shows the zone; a failing step (`vacuum.update_map` raising on `"names"`) stops before `settings` and reports `failed_kind == "names"`; `merge_rooms` drops the merged name from `RoomStore`; `split_room` adds `"<name> 2"` for the new id.
- [ ] **Step 2–4:** implement, pass. Keep every robot call on `self._runner` (device pool) and every cloud call on `self._cloud_runner`.
- [ ] **Step 5: commit** `"Controller: map editing (save, merge, split) with name remapping"`.

---

### Task 6: Map view — overlays and edit tools

**Files:**
- Modify: `src/chippy/mapmodel.py` (`MapGeometry` mm↔cell, hit tests), `src/chippy/ui/mapview.py`
- Test: `tests/test_mapmodel.py`, `tests/test_mapview.py`

**Interfaces:**
- Consumes: Task 1 (`MapZones`, `Rect`, `Line`, `MIN_SIZE_MM`).
- Produces:
  - `MapGeometry(left: int, top: int, grid_mm: int, height: int)` with `to_cell(x_mm, y_mm) -> (cx, cy)` (same transform as `live_from_frame`) and `to_mm(cx, cy) -> (int, int)`; `geometry_from_frame(frame) -> MapGeometry`.
  - `hit_zone(zones, geom, cx, cy, tolerance_cells) -> tuple[str, int, str] | None` → `(kind, index, part)` with kind `no_go|no_mop|wall`, part `body|c0|c1|c2|c3` (rect corners) or `end0|end1` (wall ends).
  - `MapView.set_zones(zones: MapZones, geom: MapGeometry | None)`, `MapView.set_order(order: list[int] | None)` (badges "1".."n" next to labels when not None).
  - `MapView.set_tool(tool: str | None)` with tools `None` (normal browsing), `"select"`, `"no_go"`, `"no_mop"`, `"wall"`, `"order"`, `"split"`, `"merge"`.
  - Signals: `"zones-edited"(object MapZones)`, `"zone-selected"(str kind, int index)` (`("", -1)` for none), `"room-picked"(int seg)` (select/order/merge tools), `"split-line"(int seg, object Line)`.
  - Behaviour per tool:
    - none: unchanged (click toggles selection, drag pans).
    - select: click a zone → `zone-selected`; drag its body moves it, a corner/end resizes; release emits `zones-edited`; click empty map → deselect; click a room → `room-picked`; Delete/BackSpace (key controller on the view, focusable) removes the selected zone → `zones-edited`; drag on empty space pans.
    - no_go / no_mop: drag draws a rectangle preview; on release, if both sides ≥ `MIN_SIZE_MM`, append and emit `zones-edited` and switch the view's selection to it.
    - wall: drag draws a line; ≥ `MIN_SIZE_MM` long → append.
    - order / merge: click a room → `room-picked`.
    - split: first click picks a room (`room-picked`), then a drag draws a line; release emits `split-line(seg, line_mm)` if ≥ `MIN_SIZE_MM`.
  - Drawing (always, also outside edit mode): no-go = red fill alpha 0.25 + red 2 px border; no-mop = accent-blue fill alpha 0.25 + blue border; walls = red 3 px lines; selected zone gets white handles (8 px squares); previews dashed.
- [ ] **Step 1: Failing unit tests** for `MapGeometry` round trip, `hit_zone` (body vs corner vs wall end vs miss), and MapView signal emission by calling the drag/click handlers directly (existing `tests/test_mapview.py` drives `_on_drag_begin/_on_drag_update/_on_click`; add `_on_drag_end` usage).
- [ ] **Step 2–4:** implement with a `Gtk.GestureDrag` `drag-end` handler, a `Gtk.EventControllerKey`, and `Gsk.PathBuilder` drawing in `do_snapshot` after the texture and before labels. Keep browsing behaviour identical when `tool is None` (existing tests must pass unchanged).
- [ ] **Step 5: commit** `"Map view: zones, walls and order overlays; edit tools"`.

---

### Task 7: Edit mode UI (header, toolbar, edit panel, Save/Cancel)

**Files:**
- Create: `src/chippy/ui/editpanel.py` (`EditPanel(Gtk.Box)`)
- Modify: `src/chippy/ui/window.py`, `src/chippy/ui/sidebar.py` ("Per room" when room settings are on)
- Test: `tests/test_editpanel.py` (widget-level, like `tests/test_preferences.py`)

**Interfaces:**
- Consumes: Tasks 1, 5, 6.
- Produces:
  - Header toggle `self.edit_button` (`Gtk.ToggleButton`, icon `document-edit-symbolic`, tooltip "Edit map"), sensitive only when `controller.can_edit`.
  - Map toolbar overlay (top-left, `osd` linked buttons, `Gtk.ToggleButton` group) with the 7 tools, visible only in edit mode; default tool `select`.
  - `EditPanel` shown in the sidebar's place (a `Gtk.Stack` with children `"controls"` and `"edit"`): "Use room settings" `Adw.SwitchRow`; when `settings_known` is False an info label with the spec copy; room panel `Adw.PreferencesGroup` titled with the room name: `Adw.EntryRow` "Name", `Adw.ComboRow` "Suction", "Mopping", `Adw.SpinRow` "Passes" (1–3), `Adw.ActionRow` "Cleaning order" subtitle "n of m"; selected-zone row with a trash button; "Reset order" button visible with the Order tool; bottom bar "Cancel" + "Save to robot" (suggested-action; insensitive when `edited.changes(original)` is empty or a save runs; `Adw.Spinner` while saving).
  - Flow: toggling Edit on → `controller.begin_edit` (spinner in the panel) → session pair `(original, edited)`; edits mutate `edited` and refresh the map overlays; Save → `controller.save_edit` → toast "Saved to robot" and leave edit mode, or a toast with the error and stay; Cancel or toggling off with changes → `Adw.AlertDialog` "Discard changes?" (Discard / Keep editing); Split/Merge → `Adw.AlertDialog` confirmation → `controller.split_room/merge_rooms` → on success rebuild the session from the new map (keep unsaved zone edits by re-applying `edited.zones`), on failure toast; a kind in `session.unsupported` disables its control with tooltip "Not supported on this model".
  - When `snapshot.room_settings` is True, the sidebar's Suction/Mopping rows show subtitle "Per room" and are insensitive.
  - Edit mode leaves automatically (with the discard prompt if needed) when `can_edit` turns False (e.g. the robot starts cleaning from the phone).
  - `MainWindow.test_state()` gains `"edit": {"mode": bool, "tool": str|None, "dirty": bool, "buttons": {tool: [x, y]…, "save": [x, y], "edit": [x, y]}, "zones": {"no_go": n, "no_mop": n, "walls": n}}`.
- [ ] **Step 1: Failing widget tests:** EditPanel shows the info label when `settings_known` is False; changing Passes marks the session dirty and enables Save; `Discard changes?` path restores the original.
- [ ] **Step 2–4:** implement; reuse `sidebar.SUCTION`/`WATER` lists.
- [ ] **Step 5: commit** `"Edit mode: toolbar, edit panel, Save/Cancel, merge/split confirmations"`.

---

### Task 8: Nested check scenario `edit` and docs

**Files:**
- Modify: `tools/probe/chippy-probe@eboye.github.io/extension.js` (`_edit` scenario), `tools/nested-check.sh` (`--scenario edit`, required checks), `README.md` (Map editing section)

- [ ] **Step 1:** Probe `_edit`: wait for main page; click `edit.buttons.edit`; check `edit.mode`; click tool `no_go`; drag a rectangle inside the living room (pointer down/move/up via the probe's existing drag helper — add `_drag(from, to)` if missing using `Clutter.VirtualInputDevice` button press, several motion steps, release); check `edit.zones.no_go === 1`; tool `wall` → drag a line → `walls === 1`; screenshot `edit-drawn`; click Save; wait `edit.mode === false`; check the map still shows 1 no-go zone and 1 wall after the refresh (`edit.zones` outside edit mode reflects the decoded map); screenshot `edit-saved`; re-enter edit, tool `merge`, click Kitchen then Hallway labels, confirm dialog with Return, check rooms count dropped by one; screenshot `edit-merged`.
- [ ] **Step 2:** required checks for `edit`: `edit-mode-on`, `no-go-drawn`, `wall-drawn`, `saved-and-shown`, `merge-done`.
- [ ] **Step 3:** run `--scenario edit` light and dark, plus `main`, `signin`, `qr` (no regressions). Look at every screenshot.
- [ ] **Step 4:** README section "Map editing" (what each tool does, Save/Cancel, that it changes the robot's map shared with the phone app).
- [ ] **Step 5: commit** `"Nested check: edit scenario; README map editing"`.

---

### Task 9: Ship and live check (user present)

- [ ] Final whole-branch review (subagent) → fix wave → re-review.
- [ ] Rebuild Flatpak, `flatpak kill`, restart; `~/.local/bin/chippy-deploy-goldie` (commits must match on all three machines).
- [ ] With the user: Edit map → add a no-go zone → Save → user checks it in the phone app → remove it → Save. Rename a room → back. Change one room's passes → back. Order → back. "Use room settings" on → off. For any kind the r2209 refuses, record it in `profiles.R2209` as unsupported (hide the control) and say so.
- [ ] Merge/split only after asking the user at that moment.
- [ ] Merge `feat/qr-signin` then `feat/map-editor` into `main` after the user's OK.
