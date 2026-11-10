import json

import pytest

from chippy.core.profiles import R2209, UnsupportedModel, is_supported, profile_for
from chippy.core.state import Snapshot


@pytest.mark.parametrize(
    ("raw", "charging", "expected"),
    [
        (1, 2, "cleaning"),
        (7, 2, "cleaning"),
        (11, 2, "cleaning"),
        (12, 2, "cleaning"),
        (3, 2, "paused"),
        (4, 2, "error"),
        (5, 2, "returning"),
        (6, 1, "charging"),
        (13, 1, "docked"),
        (2, 1, "charging"),
        (2, 2, "idle"),
        (None, None, "idle"),
    ],
)
def test_map_state(raw, charging, expected):
    assert R2209.map_state(raw, charging) == expected


def test_fault_text():
    assert R2209.fault_text(0) == ""
    assert R2209.fault_text(11) == "Dust bin full"
    assert R2209.fault_text(999) == "Fault 999"
    assert R2209.fault_text(68) == "Mopping done: clean the mop"
    assert R2209.fault_text(1000) == "Failed to return to the dock"


def test_reminder_fault_is_not_an_error_state():
    def props(status, fault, charging):
        return [
            {"siid": 2, "piid": 1, "code": 0, "value": status},
            {"siid": 2, "piid": 2, "code": 0, "value": fault},
            {"siid": 3, "piid": 2, "code": 0, "value": charging},
        ]

    snap = R2209.parse_properties(props(4, 68, 1))
    assert (snap.state, snap.fault, snap.error) == (
        "charging",
        68,
        "Mopping done: clean the mop",
    )
    assert R2209.parse_properties(props(4, 68, 0)).state == "idle"
    assert R2209.parse_properties(props(4, 47, 0)).state == "error"  # a real fault stays one


def test_parse_properties_full():
    results = [
        {"siid": 2, "piid": 1, "code": 0, "value": 13},
        {"siid": 2, "piid": 2, "code": 0, "value": 0},
        {"siid": 3, "piid": 1, "code": 0, "value": 100},
        {"siid": 3, "piid": 2, "code": 0, "value": 1},
        {"siid": 4, "piid": 2, "code": 0, "value": 63},
        {"siid": 4, "piid": 3, "code": 0, "value": 42},
        {"siid": 4, "piid": 4, "code": 0, "value": 1},
        {"siid": 4, "piid": 5, "code": 0, "value": 2},
        {"siid": 4, "piid": 6, "code": 0, "value": 0},
        {"siid": 9, "piid": 9, "code": -1},
    ]
    assert R2209.parse_properties(results) == Snapshot(
        state="docked",
        battery=100,
        charging=True,
        fault=0,
        error="",
        suction="standard",
        water="medium",
        has_water_tank=False,
        cleaned_area=42,
        cleaning_time=63,
    )


def test_room_clean_params():
    params = R2209.room_clean_params([7, 2], "strong", "low")
    assert params[0] == {"piid": 1, "value": 18}
    assert json.loads(params[1]["value"]) == {"selects": [[7, 1, 2, 1, 1], [2, 1, 2, 1, 2]]}
    assert params[1]["piid"] == 10


def test_profile_lookup():
    assert profile_for("dreame.vacuum.r2209") is R2209
    other = profile_for("dreame.vacuum.r2228")
    assert other.tested is False and other.models == ("dreame.vacuum.r2228",)
    assert other.props == R2209.props
    assert is_supported("dreame.vacuum.p2009")
    assert not is_supported("roborock.vacuum.a15")
    with pytest.raises(UnsupportedModel):
        profile_for("roborock.vacuum.a15")


def test_room_settings_flag_parsed():
    on = [{"siid": 4, "piid": 26, "code": 0, "value": 1}]
    off = [{"siid": 4, "piid": 26, "code": 0, "value": 0}]
    assert R2209.parse_properties(on).room_settings is True
    assert R2209.parse_properties(off).room_settings is False
    assert R2209.parse_properties([]).room_settings is None
    assert R2209.parse_properties([{"siid": 4, "piid": 26, "code": -1}]).room_settings is None
