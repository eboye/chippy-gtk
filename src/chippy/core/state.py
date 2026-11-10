"""Robot state shared by every model profile."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Snapshot:
    state: str = "offline"
    battery: int = 0
    charging: bool = False
    fault: int = 0
    error: str = ""
    suction: str = ""
    water: str = ""
    has_water_tank: bool = False
    cleaned_area: int = 0
    cleaning_time: int = 0
    room_settings: bool | None = None


def name_to_value(table: dict[int, str], name: str) -> int:
    for value, label in table.items():
        if label == name:
            return value
    raise ValueError(f"unknown value {name!r}; expected one of {sorted(table.values())}")
