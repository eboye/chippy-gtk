"""In-memory robot and cloud for tests and `chippy --fake`."""

import base64
import json
import math
import os
import struct
import time
import zlib
from dataclasses import dataclass, field, replace

from micloud.micloudexception import MiCloudAccessDenied

from .cloud import (
    CaptchaRequired,
    QrLogin,
    QrLoginCancelled,
    QrLoginExpired,
    TwoFactorRequired,
)
from .device import DeviceError
from .mapedit import MapZones
from .profiles import R2209
from .state import Snapshot, name_to_value

# Display orientation (top row first). Digits are room ids, '.' is outside.
# 4 is a U-shaped hallway around 3; 5 is a 2-cell sliver (hidden by default).
FAKE_LAYOUT = [
    "..........................................",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    ".22222222..1111111111111111111111111111...",
    "...22......1111111111111111111111111111...",
    ".44444444444444444444444444444444444444...",
    ".44444444444444444444444444444444444444...",
    ".444...............................4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444.33333333333333333333333333333.4444...",
    ".444......................55.......4444...",
    "..........................................",
]
FAKE_NAMES = {1: "Living room", 2: "Kitchen", 3: "Bedroom", 4: "Hallway"}
_GRID_MM = 200  # big cells so the small fake flat looks like a real one on screen


_LEFT, _TOP = -4000, -2000


def _cell_mm(col: float, row: float) -> tuple[int, int]:
    """Display cell (top row first) to absolute map mm, the inverse of the charger transform."""
    return (round(_LEFT + col * _GRID_MM), round(_TOP + (len(FAKE_LAYOUT) - row) * _GRID_MM))


NEIGHBOUR_REACH = 2  # cells: the fake leaves a one-cell wall between some rooms (3 and 4)


def layout_neighbours(layout: list[str]) -> dict[int, set[int]]:
    """Room -> the rooms with a cell within NEIGHBOUR_REACH cells of one of its own (the
    fake's `nei_id`). Rooms two cells apart or more (1 and 2) are not neighbours."""
    cells = {
        (c, r): int(ch) for r, row in enumerate(layout) for c, ch in enumerate(row) if ch.isdigit()
    }
    out: dict[int, set[int]] = {seg: set() for seg in cells.values()}
    span = range(-NEIGHBOUR_REACH, NEIGHBOUR_REACH + 1)
    for (c, r), seg in cells.items():
        for dc in span:
            for dr in span:
                other = cells.get((c + dc, r + dr))
                if other is not None and other != seg:
                    out[seg].add(other)
    return out


def _default_cleanset(layout: list[str]) -> dict[str, list[int]]:
    ids = sorted({int(ch) for row in layout for ch in row if ch.isdigit()})
    return {str(seg): [1, 2, 1, order, -1] for order, seg in enumerate(ids, 1)}


@dataclass
class FakeMap:
    """The fake robot's mutable map: edits made through FakeVacuum show up in every payload
    built from the same instance."""

    layout: list[str] = field(default_factory=lambda: list(FAKE_LAYOUT))
    names: dict[int, str] = field(default_factory=lambda: dict(FAKE_NAMES))
    zones: MapZones = field(default_factory=MapZones)
    cleanset: dict[str, list[int]] = field(default_factory=dict)
    room_settings: bool = False
    # Saved-map fetches after each edit that still return the map from before it, like the
    # cloud taking a while to publish the robot's new map (see `saved_view`).
    lag: int = 0
    _stale: "FakeMap | None" = field(default=None, init=False, repr=False, compare=False)
    _stale_left: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not self.cleanset:
            self.cleanset = _default_cleanset(self.layout)

    def before_edit(self) -> None:
        """Called before an edit is applied: with `lag`, keep the current map for that many
        saved-map fetches."""
        if self.lag > 0:
            self._stale = FakeMap(
                list(self.layout),
                dict(self.names),
                self.zones,
                {k: list(v) for k, v in self.cleanset.items()},
                self.room_settings,
            )
            self._stale_left = self.lag

    def saved_view(self) -> "FakeMap":
        """The map a saved-map fetch sees now: the pre-edit copy while lagging, else this."""
        if self.lag > 0 and self._stale is not None and self._stale_left > 0:
            self._stale_left -= 1
            return self._stale
        return self

    def ids(self) -> set[int]:
        return {int(ch) for row in self.layout for ch in row if ch.isdigit()}

    def merge(self, a: int, b: int) -> None:
        if a == b or not {a, b} <= self.ids():
            raise DeviceError("cannot merge those rooms")
        self.layout = [row.replace(str(b), str(a)) for row in self.layout]
        self.names.pop(b, None)
        self.cleanset.pop(str(b), None)

    def split(self, seg: int, line: tuple[int, int, int, int]) -> None:
        if seg not in self.ids():
            raise DeviceError("no such room")
        new = max(self.ids()) + 1
        if new > 9:  # the layout is one character per cell
            raise DeviceError("the fake map has no free room id")
        x1, y1, x2, y2 = line
        rows, moved, kept = [], 0, 0
        for r, row in enumerate(self.layout):
            chars = list(row)
            for c, ch in enumerate(chars):
                if ch != str(seg):
                    continue
                cx, cy = _cell_mm(c + 0.5, r + 0.5)
                if (x2 - x1) * (cy - y1) - (y2 - y1) * (cx - x1) > 0:
                    chars[c] = str(new)
                    moved += 1
                else:
                    kept += 1
            rows.append("".join(chars))
        if not moved or not kept:
            raise DeviceError("split line does not cross the room")
        self.layout = rows
        orders = [v[3] for v in self.cleanset.values()]
        self.cleanset[str(new)] = [1, 2, 1, max(orders, default=0) + 1, -1]


def build_fake_payload(
    robot: tuple[int, int, int] | None = None,
    path: list[tuple[int, int]] | None = None,
    fake_map: FakeMap | None = None,
    cleanset: dict[str, list[int]] | None = None,
) -> bytes:
    """The fake flat as the r2209's saved-maps document; optionally with a robot position
    (x mm, y mm, angle) in header fields 3-5, a cleaning path in the trailer's `tr` and the
    per-room `cleanset` (live frames carry it)."""
    fake_map = fake_map or FakeMap()
    layout = fake_map.layout
    width, height = len(layout[0]), len(layout)
    cells = []
    for row in reversed(layout):  # stored bottom row first
        for col, ch in enumerate(row):
            seg = int(ch) if ch.isdigit() else 0
            edge = seg and (col + 1 >= width or row[col + 1] != ch)
            cells.append(seg | (0x80 if edge else 0))
    left, top = _LEFT, _TOP
    rx, ry, angle = robot or (0, 0, 0)
    charger = (left + 37 * _GRID_MM, top + (height - 2) * _GRID_MM)
    header = struct.pack(
        "<hhBhhhhhhhhhhh",
        1,
        1,
        73,
        rx,
        ry,
        angle,
        charger[0],
        charger[1],
        180,
        _GRID_MM,
        width,
        height,
        left,
        top,
    )
    seg_inf: dict = {
        str(k): {"name": base64.b64encode(v.encode()).decode()} for k, v in fake_map.names.items()
    }
    for seg, near in layout_neighbours(layout).items():
        seg_inf.setdefault(str(seg), {})["nei_id"] = sorted(near)
    trailer: dict = {"seg_inf": seg_inf, **fake_map.zones.payload()}
    if cleanset is not None:
        trailer["cleanset"] = cleanset
    if path:
        steps = zip(path, path[1:], strict=False)
        trailer["tr"] = "S{},{}".format(*path[0]) + "".join(
            f"L{x - px},{y - py}" for (px, py), (x, y) in steps
        )
    frame = header + bytes(cells) + json.dumps(trailer).encode()
    doc = {
        "mapstr": [
            {
                "id": 0,
                "name": "",
                "angle": "0",
                "map": base64.urlsafe_b64encode(zlib.compress(frame)).decode(),
            }
        ],
        "curr_id": 0,
    }
    return json.dumps(doc).encode()


def fake_saved_payload(fake_map: FakeMap) -> bytes:
    """The saved map as the cloud serves it (honours `FakeMap.lag`)."""
    return build_fake_payload(fake_map=fake_map.saved_view())


def _live_loop() -> list[tuple[int, int]]:
    """24 waypoints, 2 cells apart, round a rectangle inside the living room (room 1):
    display rows 3-6, columns 14-35 (cell centres)."""
    points = []
    for t in range(0, 48, 2):
        if t < 21:
            col, row = 14 + t, 3
        elif t < 24:
            col, row = 35, 3 + (t - 21)
        elif t < 45:
            col, row = 35 - (t - 24), 6
        else:
            col, row = 14, 6 - (t - 45)
        points.append(_cell_mm(col + 0.5, row + 0.5))
    return points


_LIVE_LOOP = _live_loop()


def fake_live_payload(step: int, fake_map: FakeMap | None = None) -> bytes:
    """A live frame: the robot at waypoint `step % 24` of its loop round the living room,
    facing along the last segment; the path is the waypoints visited so far this lap."""
    index = step % len(_LIVE_LOOP)
    x, y = _LIVE_LOOP[index]
    px, py = _LIVE_LOOP[index - 1]  # index 0 comes from the end of the previous lap
    angle = round(math.degrees(math.atan2(y - py, x - px))) % 360
    fake_map = fake_map or FakeMap()
    return build_fake_payload(
        robot=(x, y, angle),
        path=_LIVE_LOOP[: index + 1],
        fake_map=fake_map,
        cleanset=fake_map.cleanset,
    )


class FakeVacuum:
    def __init__(self, profile=R2209, fault: int = 0, fake_map: FakeMap | None = None):
        self._profile = profile
        self.fake_map = fake_map or FakeMap()
        self.edits: list[dict] = []  # every map-update payload received
        self._snap = Snapshot(
            state="docked",
            battery=100,
            suction="standard",
            water="medium",
            has_water_tank=True,
            fault=fault,
            error=profile.fault_text(fault),
        )
        self._next: str | None = None
        self.live_step = 0  # where the fake robot is on its loop; advances each cleaning poll

    def poll(self) -> Snapshot:
        snap = replace(self._snap, room_settings=self.fake_map.room_settings)
        if snap.state == "cleaning":
            self.live_step += 1
            snap = replace(
                snap,
                cleaned_area=snap.cleaned_area + 1,
                cleaning_time=snap.cleaning_time + 1,
                battery=max(snap.battery - 1, 0),
            )
        elif self._next:
            snap = replace(snap, state=self._next, charging=self._next == "charging")
            self._next = None
        elif snap.state == "returning":
            self._next = "charging"
        self._snap = snap
        return snap

    def _set_state(self, state: str) -> None:
        self._snap = replace(self._snap, state=state, charging=False)

    def start(self) -> None:
        self._snap = replace(self._snap, cleaned_area=0, cleaning_time=0)
        self._set_state("cleaning")

    def pause(self) -> None:
        self._set_state("paused")

    def stop(self) -> None:
        self._set_state("idle")

    def dock(self) -> None:
        self._set_state("returning")

    def locate(self) -> None:
        pass

    def set_suction(self, name: str) -> None:
        name_to_value(self._profile.suction, name)
        self._snap = replace(self._snap, suction=name)

    def set_water(self, name: str) -> None:
        name_to_value(self._profile.water, name)
        self._snap = replace(self._snap, water=name)

    def clean_rooms(self, ids: list[int], suction: str, water: str) -> None:
        if not ids:
            raise DeviceError("no rooms selected")
        self.start()

    def update_map(self, payload: dict) -> None:
        fm = self.fake_map
        keys = set(payload) - {"mapid"}
        if not keys <= {"vw", "nsr", "customeClean", "cleanOrder", "msr", "dsrid"}:
            raise DeviceError("unsupported")
        self.edits.append(payload)
        fm.before_edit()
        if "vw" in payload:
            fm.zones = MapZones.parse(payload["vw"])
        for key, item in (payload.get("nsr") or {}).items():
            fm.names[int(key)] = base64.b64decode(item["name"]).decode("utf-8")
        for seg, suction, water, passes, *_ in payload.get("customeClean") or []:
            old = fm.cleanset.get(str(seg))
            order = old[3] if old else len(fm.cleanset) + 1
            fm.cleanset[str(seg)] = [suction, water, passes, order, -1]
        for order, seg in enumerate(payload.get("cleanOrder") or [], 1):
            if str(seg) in fm.cleanset:
                fm.cleanset[str(seg)][3] = order
        if "msr" in payload:
            a, b = payload["msr"]
            fm.merge(a, b)
        if "dsrid" in payload:
            *line, seg = payload["dsrid"]
            fm.split(seg, tuple(line))

    def set_room_settings_enabled(self, on: bool) -> None:
        self.fake_map.room_settings = bool(on)

    def request_map_object(self) -> str:
        return "fake-map"

    def request_live_map_object(self) -> str | None:
        return "fake-live" if self._snap.state in ("cleaning", "returning") else None


FAKE_CAPTCHA = "x23w2"
FAKE_EMAIL_CODE = "123456"


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeCloud:
    """Scripted Xiaomi cloud. The username picks the flow: 'wrong', 'captcha', '2fa'."""

    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.captcha_code: str | None = None
        self._code_sent = False
        self._verified = False

    def login(self) -> bool:
        if "wrong" in self.username:
            return False
        if "captcha" in self.username and self.captcha_code != FAKE_CAPTCHA:
            raise CaptchaRequired("fake://captcha")
        if "2fa" in self.username and not self._verified:
            raise TwoFactorRequired("fake://2fa")
        return True

    def fetch_captcha(self, url: str) -> bytes:
        return _png(120, 40, (220, 220, 220))

    def start_qr_login(self) -> QrLogin:
        return QrLogin(
            _png(160, 160, (30, 30, 30)), "https://account.xiaomi.com/fake-qr", "fake://lp", 300
        )

    def wait_qr_login(self, qr, cancelled, clock=time.monotonic, sleep=time.sleep) -> None:
        """Approves after CHIPPY_FAKE_QR_DELAY seconds (default 3), or expires with
        CHIPPY_FAKE_QR_EXPIRE=1; checks `cancelled` every 0.2 s like a real wait would."""
        delay = float(os.environ.get("CHIPPY_FAKE_QR_DELAY", "3"))
        end = clock() + delay
        while clock() < end:
            if cancelled():
                raise QrLoginCancelled()
            sleep(0.2)
        if cancelled():
            raise QrLoginCancelled()
        if os.environ.get("CHIPPY_FAKE_QR_EXPIRE") == "1":
            raise QrLoginExpired()

    def send_email_code(self, url: str) -> None:
        self._code_sent = True

    def verify_email_code(self, code: str) -> None:
        if not self._code_sent or code != FAKE_EMAIL_CODE:
            raise MiCloudAccessDenied("Wrong code")
        self._verified = True

    def get_devices(self, country=None):
        if country != "de":
            return []
        return [
            {
                "did": "1001",
                "model": "dreame.vacuum.r2209",
                "name": "Chippy",
                "localip": "127.0.0.1",
                "token": "0" * 32,
            },
            {
                "did": "1002",
                "model": "dreame.vacuum.r2228",
                "name": "Upstairs",
                "localip": "127.0.0.2",
                "token": "1" * 32,
            },
            {
                "did": "1003",
                "model": "roborock.vacuum.a15",
                "name": "Rocky",
                "localip": "127.0.0.3",
                "token": "2" * 32,
            },
            {"did": "1004", "model": "yeelink.light.color1", "name": "Lamp"},
        ]

    def request_country(self, path: str, country: str, params: dict) -> str:
        """Home and share lists: empty (the fake's robots come from `get_devices`)."""
        return json.dumps({"code": 0, "result": {}})

    def session_state(self) -> dict:
        return {"user_id": "fake"}

    def restore_session(self, state: dict) -> None:
        pass

    def map_file_url(self, object_name: str, country: str) -> str:
        return f"fake://map/{object_name}"
