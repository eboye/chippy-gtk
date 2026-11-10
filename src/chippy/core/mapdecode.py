"""Decode a Dreame cloud map file into a MapFrame: the room (segment) pixel grid and its
geometry, room names, charger and robot position, and the cleaning path."""

import base64
import binascii
import json
import re
import struct
import zlib
from dataclasses import dataclass, field

# Segment ids travel over D-Bus as `u` (uint32); 0 is not a real room.
SEGMENT_ID_LIMIT = 2**32
HEADER = struct.Struct("<hhBhhhhhhhhhhh")  # 27 bytes
ROOM_TYPES = {
    1: "Living room",
    2: "Primary bedroom",
    3: "Study",
    4: "Kitchen",
    5: "Dining hall",
    6: "Bathroom",
    7: "Balcony",
    8: "Corridor",
    9: "Utility room",
    10: "Closet",
    11: "Meeting room",
    12: "Office",
    13: "Fitness area",
    14: "Recreation area",
    15: "Secondary bedroom",
}


class MapDecodeError(Exception):
    pass


def _inflate(payload: bytes | str) -> bytes:
    data = payload.encode() if isinstance(payload, str) else payload
    if data[:1] == b"\x78":
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    text = data.decode("ascii", errors="strict").strip().replace("_", "/").replace("-", "+")
    text += "=" * (-len(text) % 4)
    try:
        return zlib.decompress(base64.b64decode(text, validate=True))
    except (binascii.Error, zlib.error, ValueError) as err:
        raise MapDecodeError(f"map payload is not base64+zlib (encrypted map?): {err}") from err


def _name(seg_id: int, item: dict) -> str:
    raw_name = item.get("name")
    if raw_name:
        try:
            decoded = base64.b64decode(raw_name).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError):
            decoded = str(raw_name).strip()
        if decoded:
            return decoded
    return ROOM_TYPES.get(int(item.get("type") or 0), f"Room {seg_id}")


def _unwrap_saved_maps(payload: bytes | str) -> bytes | str:
    """The r2209's cloud object is a saved-maps document:
    {"mapstr": [{"id": 0, "name": "", "angle": "0", "map": "<base64 zlib frame>"}], "curr_id": 3}.
    Return the frame of the current map (or the first one); anything else passes through."""
    head = payload[:1] if isinstance(payload, bytes) else payload[:1].encode()
    if head != b"{":
        return payload
    try:
        doc = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as err:
        raise MapDecodeError(f"saved-maps document is not JSON: {err}") from err
    maps = doc.get("mapstr") if isinstance(doc, dict) else None
    if not isinstance(maps, list) or not maps:
        raise MapDecodeError("saved-maps document has no maps")
    entries = [m for m in maps if isinstance(m, dict) and isinstance(m.get("map"), str)]
    if not entries:
        raise MapDecodeError("saved-maps document has no map data")
    current = [m for m in entries if m.get("id") == doc.get("curr_id")]
    return (current or entries)[0]["map"]


@dataclass(frozen=True)
class MapFrame:
    width: int
    height: int
    grid_mm: int
    left: int
    top: int
    charger: tuple[int, int] | None
    pixels: bytes  # width*height cells, stored bottom row first
    names: dict[int, str]
    robot: tuple[int, int, int] | None = None  # x mm, y mm, angle in degrees (map frame, y up)
    path: list[tuple[int, int]] = field(default_factory=list)  # cleaning path, absolute mm


_PATH_TOKEN = re.compile(r"([SMLl])\s*(-?\d+)\s*,\s*(-?\d+)")


def parse_path(tr: str) -> list[tuple[int, int]]:
    """Dreame trajectory, as the r2209 sends it: `S x,y` (start), `M x,y` and `l x,y` are
    absolute points; `L dx,dy` is a step relative to the previous point. Separators are
    optional; anything that isn't one of those commands is skipped."""
    points: list[tuple[int, int]] = []
    for cmd, a, b in _PATH_TOKEN.findall(tr):
        x, y = int(a), int(b)
        if cmd == "L":
            if not points:
                continue
            x, y = points[-1][0] + x, points[-1][1] + y
        points.append((x, y))
    return points


def decode_map(payload: bytes | str) -> MapFrame:
    payload = _unwrap_saved_maps(payload)
    try:
        raw = _inflate(payload)
    except UnicodeDecodeError as err:
        raise MapDecodeError(f"map payload is binary but not zlib: {err}") from err
    if len(raw) < HEADER.size:
        raise MapDecodeError("map is shorter than its header")
    fields = HEADER.unpack_from(raw)
    width, height = fields[10], fields[11]
    if width < 0 or height < 0:
        raise MapDecodeError("map header has a negative size")
    grid_mm, left, top = fields[9], fields[12], fields[13]
    if grid_mm <= 0:
        raise MapDecodeError("map header has no grid size")
    end = HEADER.size + width * height
    if len(raw) < end:
        raise MapDecodeError("map pixels are truncated")
    pixels = raw[HEADER.size : end]
    trailer = raw[end:]
    if not trailer.strip():
        raise MapDecodeError("map has no room data")
    try:
        info = json.loads(trailer.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as err:
        raise MapDecodeError(f"map room data is not JSON: {err}") from err
    if not isinstance(info, dict):
        raise MapDecodeError("map room data is not a JSON object")
    seg_inf = info.get("seg_inf") or {}
    if not isinstance(seg_inf, dict):
        raise MapDecodeError("map seg_inf is not a JSON object")
    try:
        names = {
            int(key): _name(int(key), item or {})
            for key, item in seg_inf.items()
            if 0 < int(key) < SEGMENT_ID_LIMIT
        }
    except (AttributeError, TypeError, ValueError, OverflowError) as err:
        raise MapDecodeError(f"map room data is malformed: {err}") from err
    charger = (fields[6], fields[7]) if (fields[6], fields[7]) != (0, 0) else None
    robot = (fields[3], fields[4], fields[5]) if (fields[3], fields[4]) != (0, 0) else None
    trail = info.get("tr")
    path = parse_path(trail) if isinstance(trail, str) else []
    return MapFrame(width, height, grid_mm, left, top, charger, bytes(pixels), names, robot, path)
