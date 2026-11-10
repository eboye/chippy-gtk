"""Map editing data: zones and walls, per-room settings, cleaning order, and the exact
payloads Dreame robots take on action 6.2 (see the spec for the protocol)."""

import base64
import json
from dataclasses import dataclass, field, replace

from .state import name_to_value

MIN_SIZE_MM = 300
# Segment ids travel over D-Bus as `u` (uint32); 0 is not a real room.
SEGMENT_ID_LIMIT = 2**32
# Labels of the save steps, for messages ("Saved zones; room names failed: ...").
STEP_LABELS = {
    "zones": "zones",
    "names": "room names",
    "settings": "room settings",
    "order": "cleaning order",
    "room_settings": "Use room settings",
}


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
        return cls.read(vw)[0]

    @classmethod
    def read(cls, vw: object) -> tuple["MapZones", bool]:
        """(zones, known): `known` only when `vw` is an object and every item of its
        line/rect/mop lists was read. Zones that weren't all read must never be resent (the
        robot takes the complete set, so a save would drop the unread ones)."""
        if not isinstance(vw, dict):
            return cls(), False
        known = True

        def values(key) -> list[list[int]]:
            nonlocal known
            items = vw.get(key, [])
            if not isinstance(items, list):
                known = False
                return []
            out = [_ints(i, 4) for i in items]
            known = known and all(v is not None for v in out)
            return [v for v in out if v is not None]

        zones = cls(
            tuple(Rect.of(*v) for v in values("rect")),
            tuple(Rect.of(*v) for v in values("mop")),
            tuple(Line(*v) for v in values("line")),
        )
        return zones, known

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
        if not (isinstance(key, str) and key.isdecimal()) or not isinstance(values, list):
            continue
        if len(values) < 3 or not 0 < int(key) < SEGMENT_ID_LIMIT:
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
    zones_known: bool = True  # False: the map's zones couldn't all be read; never resend them

    @property
    def zones_locked(self) -> bool:
        """Zones are read-only: the robot refused them, or they couldn't be read."""
        return "zones" in self.unsupported or not self.zones_known

    def renamed(self, original: "EditSession") -> dict[int, str]:
        """Rooms whose (non-blank) name differs from `original`'s, with the stripped name."""
        return {
            seg: name.strip()
            for seg, name in sorted(self.names.items())
            if name.strip() and name.strip() != original.names.get(seg)
        }

    def copy(self) -> "EditSession":
        return replace(
            self,
            names=dict(self.names),
            settings=dict(self.settings),
            order=list(self.order),
            unsupported=set(self.unsupported),
        )

    def changes(self, original: "EditSession", profile) -> list[MapEdit]:
        """The steps to send, in a fixed order; kinds the robot refused as unsupported are
        left out (their staged edits stay, but can't be sent)."""
        edits: list[MapEdit] = []
        off = self.unsupported
        if not self.zones_locked and self.zones != original.zones:
            edits.append(MapEdit("zones", self.zones.payload()))
        renamed = self.renamed(original) if "names" not in off else {}
        if renamed:
            edits.append(MapEdit("names", rename_payload(self.map_id, renamed)))
        # only what the user changed: unknown settings are never sent as defaults
        if "settings" not in off and self.settings and self.settings != original.settings:
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
        if "order" not in off and self.order and self.order != original.order:
            edits.append(MapEdit("order", {"cleanOrder": list(self.order)}))
        if "room_settings" not in off and self.use_room_settings != original.use_room_settings:
            edits.append(MapEdit("room_settings", None, self.use_room_settings))
        return edits

    def saved(self, edited: "EditSession", kinds) -> "EditSession":
        """A copy of this original with the `kinds` of `edited` that reached the robot, so
        they are neither resent nor counted as changes."""
        out = self.copy()
        if "zones" in kinds:
            out.zones = edited.zones
        if "names" in kinds:
            out.names.update(edited.renamed(self))
        if "settings" in kinds:
            out.settings = dict(edited.settings)
        if "order" in kinds:
            out.order = list(edited.order)
        if "room_settings" in kinds:
            out.use_room_settings = edited.use_room_settings
        return out


def save_failure_text(error: Exception, failed: str | None, done: list[str]) -> str:
    """'Saved zones; room names failed: <error>' (or 'Saving room names failed: ...')."""
    if failed is None:
        return f"Saving failed: {error}"
    label = STEP_LABELS.get(failed, failed)
    if not done:
        return f"Saving {label} failed: {error}"
    labels = [STEP_LABELS.get(kind, kind) for kind in done]
    saved = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return f"Saved {saved}; {label} failed: {error}"


def are_neighbours(neighbours: dict[int, frozenset[int]], a: int, b: int) -> bool:
    """Whether rooms a and b touch (the map's `nei_id`); any pair when it has no data on them."""
    if a not in neighbours and b not in neighbours:
        return True
    return b in neighbours.get(a, frozenset()) or a in neighbours.get(b, frozenset())


def carry_edits(before: EditSession, edited: EditSession, fresh: EditSession) -> EditSession:
    """A copy of `fresh` (read after a merge or split) with the unsaved edits of `edited`
    (staged against `before`) carried over: zones, and for rooms that still exist their
    names and settings, the order (survivors as edited, new rooms after) and the switch."""
    out = fresh.copy()
    out.zones = edited.zones
    for seg, name in edited.names.items():
        if seg in out.names and name != before.names.get(seg):
            out.names[seg] = name
    for seg, setting in edited.settings.items():
        if seg in out.settings and setting != before.settings.get(seg):
            out.settings[seg] = setting
    if edited.order != before.order:
        kept = [seg for seg in edited.order if seg in out.order]
        out.order = kept + [seg for seg in out.order if seg not in kept]
    if edited.use_room_settings != before.use_room_settings:
        out.use_room_settings = edited.use_room_settings
    out.unsupported |= edited.unsupported
    return out


def rename_payload(map_id: int, names: dict[int, str]) -> dict:
    """Custom room names on the robot (`nsr`): base64 UTF-8 name, type 0 (custom)."""
    nsr = {
        str(seg): {
            "name": base64.b64encode(name.encode("utf-8")).decode(),
            "type": 0,
            "index": 0,
        }
        for seg, name in sorted(names.items())
    }
    return {"nsr": nsr, "mapid": map_id}


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
