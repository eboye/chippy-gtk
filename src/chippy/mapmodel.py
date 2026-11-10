"""Pure map geometry: display grid, rooms, label anchors, zoom/pan, texture pixels."""

import math
from collections import deque
from dataclasses import dataclass

from .core.mapdecode import MapFrame
from .core.mapedit import MapZones, Rect

MIN_VISIBLE_AREA = 0.25
PALETTE = [
    "#5bc8af",
    "#f6d32d",
    "#cdab8f",
    "#99c1f1",
    "#ffa348",
    "#62a0ea",
    "#57e389",
    "#f66151",
    "#dc8add",
    "#e5a50a",
    "#8ff0a4",
    "#c061cb",
]
_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@dataclass(frozen=True)
class MapGrid:
    width: int
    height: int
    grid_mm: int
    cells: bytes
    charger: tuple[float, float] | None

    def seg_at(self, col: int, row: int) -> int:
        if 0 <= col < self.width and 0 <= row < self.height:
            return self.cells[row * self.width + col]
        return 0

    def cell_area(self) -> float:
        return (self.grid_mm / 1000) ** 2


def grid_from_frame(frame: MapFrame) -> MapGrid:
    w, h = frame.width, frame.height
    rows = [frame.pixels[r * w : (r + 1) * w] for r in range(h)]
    cells = bytes(p & 0x3F for row in reversed(rows) for p in row)
    charger = None
    if frame.charger:
        cx = (frame.charger[0] - frame.left) / frame.grid_mm
        cy = h - (frame.charger[1] - frame.top) / frame.grid_mm
        charger = (cx, cy)
    return MapGrid(w, h, frame.grid_mm, cells, charger)


def room_areas(grid: MapGrid) -> dict[int, int]:
    counts: dict[int, int] = {}
    for seg in grid.cells:
        if seg:
            counts[seg] = counts.get(seg, 0) + 1
    return counts


def tiny_rooms(grid: MapGrid) -> set[int]:
    area = grid.cell_area()
    return {seg for seg, n in room_areas(grid).items() if n * area < MIN_VISIBLE_AREA}


def label_anchors(grid: MapGrid) -> dict[int, tuple[float, float]]:
    """Per room, the centre of the cell farthest from the room's edge (inside U shapes too)."""
    w, h = grid.width, grid.height
    dist = [-1] * (w * h)
    queue: deque[tuple[int, int]] = deque()
    for row in range(h):
        for col in range(w):
            seg = grid.seg_at(col, row)
            if not seg or any(grid.seg_at(col + dx, row + dy) != seg for dx, dy in _STEPS):
                dist[row * w + col] = 0
                queue.append((col, row))
    while queue:
        col, row = queue.popleft()
        for dx, dy in _STEPS:
            nc, nr = col + dx, row + dy
            if 0 <= nc < w and 0 <= nr < h and dist[nr * w + nc] < 0:
                dist[nr * w + nc] = dist[row * w + col] + 1
                queue.append((nc, nr))
    deepest: dict[int, int] = {}
    sums: dict[int, list[int]] = {}  # seg -> [cells, sum of cols, sum of rows]
    for row in range(h):
        for col in range(w):
            seg = grid.seg_at(col, row)
            if not seg:
                continue
            deepest[seg] = max(deepest.get(seg, 0), dist[row * w + col])
            acc = sums.setdefault(seg, [0, 0, 0])
            acc[0], acc[1], acc[2] = acc[0] + 1, acc[1] + col, acc[2] + row
    # Among the deepest cells, the one nearest the room's centroid (ties: first in scan order),
    # so a label sits in the middle of a rectangle rather than at its top-left deepest cell.
    best: dict[int, tuple[float, int, int]] = {}
    for row in range(h):
        for col in range(w):
            seg = grid.seg_at(col, row)
            if not seg or dist[row * w + col] != deepest[seg]:
                continue
            n, sum_col, sum_row = sums[seg]
            gap = (col - sum_col / n) ** 2 + (row - sum_row / n) ** 2
            if seg not in best or gap < best[seg][0]:
                best[seg] = (gap, col, row)
    return {seg: (col + 0.5, row + 0.5) for seg, (_, col, row) in best.items()}


@dataclass(frozen=True)
class LiveState:
    """The robot while it cleans, in display cells (like `MapGrid.charger`).

    `angle` is the robot's raw heading in degrees (map frame: 0 = right, counter-clockwise
    positive). The display flips y, so MapView draws the heading as (cos a, -sin a) in its
    y-down widget coordinates. Unverified against the real robot."""

    robot: tuple[float, float] | None
    angle: float
    path: list[tuple[float, float]]


def live_from_frame(live: MapFrame, saved: MapFrame, grid: MapGrid) -> LiveState:
    """Place a live frame's robot and path on the saved map. Positions are absolute map mm, so
    they convert with the saved frame's geometry even when the live frame is a different crop."""

    def cell(x: int, y: int) -> tuple[float, float]:
        return (
            (x - saved.left) / saved.grid_mm,
            saved.height - (y - saved.top) / saved.grid_mm,
        )

    robot = cell(live.robot[0], live.robot[1]) if live.robot else None
    angle = float(live.robot[2]) if live.robot else 0.0
    return LiveState(robot, angle, [cell(x, y) for x, y in live.path])


@dataclass(frozen=True)
class MapGeometry:
    """Map mm (y up) <-> display cells (rows top first), the transform of `live_from_frame`."""

    left: int
    top: int
    grid_mm: int
    height: int

    def to_cell(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        return (x_mm - self.left) / self.grid_mm, self.height - (y_mm - self.top) / self.grid_mm

    def to_mm(self, cx: float, cy: float) -> tuple[int, int]:
        return (
            round(self.left + cx * self.grid_mm),
            round(self.top + (self.height - cy) * self.grid_mm),
        )


def geometry_from_frame(frame: MapFrame) -> MapGeometry:
    return MapGeometry(frame.left, frame.top, frame.grid_mm, frame.height)


def rect_corners(rect: Rect) -> list[tuple[int, int]]:
    """Corners c0..c3 in mm: (x1, y1), (x2, y1), (x2, y2), (x1, y2)."""
    return [(rect.x1, rect.y1), (rect.x2, rect.y1), (rect.x2, rect.y2), (rect.x1, rect.y2)]


def _segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def hit_zone(
    zones: MapZones, geom: MapGeometry, cx: float, cy: float, tolerance_cells: float
) -> tuple[str, int, str] | None:
    """The zone part under display cell (cx, cy): (kind, index, part).

    Drawn bottom to top as no-mop, no-go, walls, each in list order, so the topmost is the
    last wall, then the last no-go, ... Handles (rect corners, wall ends) of any zone beat
    every body. A handle's reach is capped at a third of the zone's smaller side (a wall's
    length), so a small zone seen zoomed out still has a body to grab."""
    stack = [("wall", i, w) for i, w in reversed(list(enumerate(zones.walls)))]
    stack += [("no_go", i, r) for i, r in reversed(list(enumerate(zones.no_go)))]
    stack += [("no_mop", i, r) for i, r in reversed(list(enumerate(zones.no_mop)))]

    def near(point, reach) -> bool:
        x, y = geom.to_cell(*point)
        return abs(x - cx) <= reach and abs(y - cy) <= reach

    for kind, index, shape in stack:
        if kind == "wall":
            ends = [(shape.x1, shape.y1), (shape.x2, shape.y2)]
            size = math.hypot(shape.x2 - shape.x1, shape.y2 - shape.y1)
            parts = [(end, f"end{n}") for n, end in enumerate(ends)]
        else:
            size = min(shape.x2 - shape.x1, shape.y2 - shape.y1)
            parts = [(corner, f"c{n}") for n, corner in enumerate(rect_corners(shape))]
        reach = min(tolerance_cells, size / geom.grid_mm / 3)
        for point, part in parts:
            if near(point, reach):
                return kind, index, part
    for kind, index, shape in stack:
        if kind == "wall":
            a, b = geom.to_cell(shape.x1, shape.y1), geom.to_cell(shape.x2, shape.y2)
            if _segment_distance(cx, cy, *a, *b) <= tolerance_cells:
                return kind, index, "body"
        else:
            (x1, y1), (x2, y2) = geom.to_cell(shape.x1, shape.y1), geom.to_cell(shape.x2, shape.y2)
            if min(x1, x2) <= cx <= max(x1, x2) and min(y1, y2) <= cy <= max(y1, y2):
                return kind, index, "body"
    return None


@dataclass(frozen=True)
class Room:
    id: int
    name: str
    area_m2: float | None
    color: str
    anchor: tuple[float, float] | None
    hidden: bool


def build_rooms(
    grid: MapGrid | None,
    cloud_names: dict[int, str],
    user_names: dict[int, str],
    hidden: set[int],
) -> list[Room]:
    counts = room_areas(grid) if grid else {}
    anchors = label_anchors(grid) if grid else {}
    ids = sorted(set(counts) | set(cloud_names) | set(user_names))
    rooms = []
    for index, seg in enumerate(ids):
        name = user_names.get(seg) or cloud_names.get(seg) or f"Room {seg}"
        area = round(counts[seg] * grid.cell_area(), 1) if grid and seg in counts else None
        rooms.append(
            Room(seg, name, area, PALETTE[index % len(PALETTE)], anchors.get(seg), seg in hidden)
        )
    return rooms


class Viewport:
    MARGIN = 0.92

    def __init__(self, map_w: int, map_h: int):
        self.map_w, self.map_h = map_w, map_h
        self.width = self.height = 1.0
        self.scale, self.ox, self.oy, self.fitted = 1.0, 0.0, 0.0, True

    def fit_scale(self) -> float:
        return min(self.width / self.map_w, self.height / self.map_h) * self.MARGIN

    def fit(self) -> None:
        self.scale = self.fit_scale()
        self.ox = (self.width - self.map_w * self.scale) / 2
        self.oy = (self.height - self.map_h * self.scale) / 2
        self.fitted = True

    def resize(self, width: float, height: float) -> None:
        self.width, self.height = max(width, 1.0), max(height, 1.0)
        if self.fitted:
            self.fit()

    def zoom_at(self, factor: float, px: float, py: float) -> None:
        cx, cy = self.to_cell(px, py)
        fit = self.fit_scale()
        self.scale = min(max(self.scale * factor, fit * 0.5), fit * 8)
        self.ox, self.oy = px - cx * self.scale, py - cy * self.scale
        self.fitted = False

    def pan(self, dx: float, dy: float) -> None:
        self.ox += dx
        self.oy += dy
        self.fitted = False

    def to_cell(self, px: float, py: float) -> tuple[float, float]:
        return (px - self.ox) / self.scale, (py - self.oy) / self.scale

    def to_widget(self, cx: float, cy: float) -> tuple[float, float]:
        return self.ox + cx * self.scale, self.oy + cy * self.scale


Rgb = tuple[int, int, int]


def _rgb(hex_color: str) -> Rgb:
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))


def _mix(a: Rgb, b: Rgb, t: float) -> Rgb:
    return (
        int(a[0] * t + b[0] * (1 - t)),
        int(a[1] * t + b[1] * (1 - t)),
        int(a[2] * t + b[2] * (1 - t)),
    )


def texture_rgba(grid: MapGrid, rooms: list[Room], selected: set[int], background: Rgb) -> bytes:
    colours: dict[int, Rgb] = {}
    for room in rooms:
        base = _rgb(room.color)
        if room.hidden:
            base = _mix((128, 128, 128), background, 0.45)
        elif room.id not in selected:
            base = _mix(base, background, 0.55)
        colours[room.id] = base
    out = bytearray(grid.width * grid.height * 4)
    for row in range(grid.height):
        for col in range(grid.width):
            seg = grid.seg_at(col, row)
            if not seg:
                continue
            r, g, b = colours.get(seg, (128, 128, 128))
            if any(grid.seg_at(col + dx, row + dy) != seg for dx, dy in _STEPS):
                r, g, b = _mix((r, g, b), background, 0.7)  # outline between rooms
            i = (row * grid.width + col) * 4
            out[i : i + 4] = bytes((r, g, b, 255))
    return bytes(out)
