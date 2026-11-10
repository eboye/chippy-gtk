"""In-memory robot and cloud for tests and `chippy --fake`."""

import base64
import json
import math
import struct
import zlib
from dataclasses import replace

from micloud.micloudexception import MiCloudAccessDenied

from .cloud import CaptchaRequired, TwoFactorRequired
from .device import DeviceError
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


def build_fake_payload(
    robot: tuple[int, int, int] | None = None, path: list[tuple[int, int]] | None = None
) -> bytes:
    """The fake flat as the r2209's saved-maps document; optionally with a robot position
    (x mm, y mm, angle) in header fields 3-5 and a cleaning path in the trailer's `tr`."""
    width, height = len(FAKE_LAYOUT[0]), len(FAKE_LAYOUT)
    cells = []
    for row in reversed(FAKE_LAYOUT):  # stored bottom row first
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
    seg_inf = {
        str(k): {"name": base64.b64encode(v.encode()).decode()} for k, v in FAKE_NAMES.items()
    }
    seg_inf["5"] = {}
    trailer: dict = {"seg_inf": seg_inf}
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


def fake_live_payload(step: int) -> bytes:
    """A live frame: the robot at waypoint `step % 24` of its loop round the living room,
    facing along the last segment; the path is the waypoints visited so far this lap."""
    index = step % len(_LIVE_LOOP)
    x, y = _LIVE_LOOP[index]
    px, py = _LIVE_LOOP[index - 1]  # index 0 comes from the end of the previous lap
    angle = round(math.degrees(math.atan2(y - py, x - px))) % 360
    return build_fake_payload(robot=(x, y, angle), path=_LIVE_LOOP[: index + 1])


class FakeVacuum:
    def __init__(self, profile=R2209, fault: int = 0):
        self._profile = profile
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
        snap = self._snap
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

    def session_state(self) -> dict:
        return {"user_id": "fake"}

    def restore_session(self, state: dict) -> None:
        pass

    def map_file_url(self, object_name: str, country: str) -> str:
        return f"fake://map/{object_name}"
