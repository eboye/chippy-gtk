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
