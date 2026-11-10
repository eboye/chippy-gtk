"""Everything model-specific. Nothing outside this module knows a siid/piid."""

import json
from dataclasses import dataclass, replace

from .state import Snapshot, name_to_value


class UnsupportedModel(Exception):
    pass


# MIoT error codes that mean "this device has no such action/property" (not "it failed this
# time"): only these mark a map-edit kind as unsupported on the model.
MIOT_UNSUPPORTED_CODES = frozenset({-4001, -4002, -4003, -4004})


@dataclass(frozen=True, eq=False)
class Profile:
    key: str
    models: tuple[str, ...]
    tested: bool
    props: dict[str, tuple[int, int]]
    actions: dict[str, tuple[int, int]]
    cleaning_statuses: frozenset[int]
    statuses: dict[int, str]
    suction: dict[int, str]
    water: dict[int, str]
    faults: dict[int, str]
    attention_faults: frozenset[int]
    map_info_prop: tuple[int, int] | None
    map_format: str  # "plain" (base64+zlib frame) or "encrypted" (not supported yet)
    segment_clean_mode: int
    # faults that are only reminders ("mopping done, clean the mop"): no error, no alarm
    reminder_faults: frozenset[int] = frozenset()
    map_update_piid: int | None = None

    def map_state(self, raw_status: int | None, charging: int | None) -> str:
        if raw_status in self.cleaning_statuses:
            return "cleaning"
        if raw_status in self.statuses:
            return self.statuses[raw_status]
        return "charging" if charging == 1 else "idle"

    def fault_severity(self, code: int) -> str:
        """ "" (no fault), "reminder", "attention" (bin, tank: the user can fix it) or "error"."""
        if not code:
            return ""
        if code in self.reminder_faults:
            return "reminder"
        return "attention" if code in self.attention_faults else "error"

    def fault_text(self, code: int) -> str:
        if not code:
            return ""
        return self.faults.get(code, f"Fault {code}")

    def parse_properties(self, results: list[dict]) -> Snapshot:
        lookup = {ids: key for key, ids in self.props.items()}
        values: dict[str, object] = {}
        for item in results:
            if item.get("code", 0) != 0:
                continue
            key = lookup.get((item.get("siid"), item.get("piid")))
            if key is not None:
                values[key] = item.get("value")
        fault = int(values.get("fault") or 0)
        state = self.map_state(values.get("status"), values.get("charging"))
        if state == "error" and fault in self.reminder_faults:
            # a reminder ("clean the mop") is not a breakdown: show where the robot is instead
            state = "charging" if values.get("charging") == 1 else "idle"
        return Snapshot(
            state=state,
            battery=int(values.get("battery") or 0),
            charging=values.get("charging") == 1,
            fault=fault,
            error=self.fault_text(fault),
            suction=self.suction.get(values.get("suction"), ""),
            water=self.water.get(values.get("water"), ""),
            has_water_tank=values.get("water_tank") == 1,
            cleaned_area=int(values.get("cleaned_area") or 0),
            cleaning_time=int(values.get("cleaning_time") or 0),
            room_settings=(values["room_settings"] == 1) if "room_settings" in values else None,
        )

    def room_clean_params(self, ids: list[int], suction: str, water: str) -> list[dict]:
        s_val = name_to_value(self.suction, suction)
        w_val = name_to_value(self.water, water)
        selects = [[int(seg), 1, s_val, w_val, order] for order, seg in enumerate(ids, start=1)]
        payload = json.dumps({"selects": selects}, separators=(",", ":"))
        return [{"piid": 1, "value": self.segment_clean_mode}, {"piid": 10, "value": payload}]


# dreame.vacuum.r2209 (urn:miot-spec-v2:device:vacuum:0000A006:dreame-r2209:2), live-verified.
# Dreame fault codes (MIoT 2.2), shared by Dreame robots. Codes and meanings follow the
# open-source dreame-vacuum integration (Tasshack/dreame-vacuum, types.py + en.json).
DREAME_FAULTS: dict[int, str] = {
    1: "Wheel lifted",
    2: "Cliff sensor error",
    3: "Bumper stuck",
    4: "Robot is tilted",
    5: "Bumper stuck",
    6: "Wheel lifted",
    7: "Optical flow sensor error",
    8: "Dust bin missing",
    9: "Water tank missing",
    10: "Water tank empty",
    11: "Dust bin full",
    12: "Main brush tangled",
    13: "Side brush tangled",
    14: "Fan error",
    15: "Left wheel error",
    16: "Right wheel error",
    17: "Robot is stuck and can't turn",
    18: "Robot is stuck and can't go forward",
    19: "Cannot find the dock",
    20: "Battery low",
    21: "Charging error",
    22: "Battery level error",
    23: "Internal error",
    24: "Visual positioning sensor error",
    25: "Movement sensor error",
    26: "Optical sensor error",
    27: "Infrared sensor blocked",
    28: "The dock has no power",
    29: "Battery error",
    30: "Fan speed sensor error",
    31: "Left wheel may be blocked",
    32: "Right wheel may be blocked",
    33: "Accelerometer error",
    34: "Gyro error",
    35: "Gyro error",
    36: "Left magnet sensor error",
    37: "Right magnet sensor error",
    38: "Flow sensor error",
    39: "Infrared error",
    40: "Camera error",
    41: "Strong magnetic field detected",
    42: "Water pump error",
    43: "Clock error",
    44: "Internal error",
    45: "Internal error",
    46: "Internal error",
    47: "Robot is blocked",
    48: "Laser distance sensor error",
    49: "Laser distance sensor bumper error",
    50: "Water pump error",
    51: "Filter wet or blocked",
    54: "Edge sensor error",
    55: "Start the robot off the carpet",
    56: "3D obstacle sensor error",
    57: "Edge sensor error",
    58: "Ultrasonic sensor error",
    59: "No-go zone or virtual wall in the way",
    61: "Cleaning route is blocked",
    62: "Cleaning route is blocked",
    63: "Robot is blocked",
    64: "Robot is blocked",
    65: "Robot is in a restricted area",
    66: "Robot is in a restricted area",
    67: "Robot is in a restricted area",
    68: "Mopping done: clean the mop",
    69: "The mop pad came off",
    70: "The mop pad came off",
    71: "The mop pad stopped rotating",
    72: "The mop pad stopped rotating",
    74: "Mop pad installation failed",
    75: "Battery almost empty: shutting down soon",
    76: "Used water tank not installed",
    78: "Robot is in a hidden area",
    79: "Laser sensor failed to lift",
    80: "Laser sensor can't rise here",
    81: "Laser sensor can't rise here",
    82: "Slippery floor",
    85: "Check that the mop is installed properly",
    86: "Used water tank level is abnormal",
    88: "Retractable legs may be tangled",
    89: "Internal error: restart the robot",
    90: "Laser sensor can't rise here",
    91: "Stuck among tables and chairs",
    92: "Stuck in a narrow passage",
    93: "Stuck at a step or threshold",
    94: "Stuck in a low-clearance area",
    95: "Ramp detected on the path",
    96: "Obstacle on the path",
    97: "Person or pet on the path",
    98: "Stuck: wheels slipping",
    99: "Slipping on the carpet",
    101: "Dust bag full or air duct blocked",
    102: "Dock cover open or dust bag missing",
    103: "Dock cover open or dust bag missing",
    104: "Dust bag full or air duct blocked",
    105: "Clean water tank not installed",
    106: "Dirty water tank full or not installed",
    107: "Clean water tank is low",
    108: "Dirty water tank full or not installed",
    109: "Dirty water tank blocked",
    110: "Dirty water tank pump error",
    111: "Washboard not installed properly",
    112: "Washboard water level abnormal",
    114: "Cleaning done: clean the mop washboard",
    116: "Fill the clean water tank",
    117: "The dock has no power",
    118: "Used water tank level too high",
    119: "Washboard water level too high",
    120: "Mop pad not in the dock",
    121: "Dust bag full or vents blocked",
    123: "Water module self-test failed",
    124: "Washboard stopped working",
    125: "Used water tank drainage failed",
    126: "Mop not detected",
    127: "Mop holder error in the dock",
    128: "Dock error",
    129: "Failed to wash the mop",
    1000: "Failed to return to the dock",
}

R2209 = Profile(
    key="r2209",
    models=("dreame.vacuum.r2209",),
    tested=True,
    props={
        "status": (2, 1),
        "fault": (2, 2),
        "battery": (3, 1),
        "charging": (3, 2),
        "cleaning_time": (4, 2),
        "cleaned_area": (4, 3),
        "suction": (4, 4),
        "water": (4, 5),
        "water_tank": (4, 6),
        "room_settings": (4, 26),
    },
    actions={
        "start": (2, 1),
        "pause": (2, 2),
        "stop": (4, 2),
        "dock": (3, 1),
        "locate": (7, 1),
        "clean": (4, 1),
        "map_request": (6, 1),
        "map_update": (6, 2),
    },
    cleaning_statuses=frozenset({1, 7, 11, 12}),
    statuses={3: "paused", 4: "error", 5: "returning", 6: "charging", 13: "docked"},
    suction={0: "quiet", 1: "standard", 2: "strong", 3: "turbo"},
    water={1: "low", 2: "medium", 3: "high"},
    faults=DREAME_FAULTS,
    attention_faults=frozenset({8, 9, 10, 11}),
    reminder_faults=frozenset({68, 114}),
    map_info_prop=(6, 8),
    map_format="plain",
    segment_clean_mode=18,
    map_update_piid=4,
)

PROFILES: tuple[Profile, ...] = (R2209,)
_DREAME = "dreame.vacuum."


def is_supported(model: str) -> bool:
    return model.startswith(_DREAME)


def profile_for(model: str) -> Profile:
    for profile in PROFILES:
        if model in profile.models:
            return profile
    if is_supported(model):
        # Untested Dreame model: most share the r2209's MIoT layout.
        return replace(R2209, key=f"{R2209.key}-generic", models=(model,), tested=False)
    raise UnsupportedModel(model)
