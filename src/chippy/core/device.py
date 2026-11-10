"""Synchronous wrapper around a python-miio MiotDevice for a profiled Dreame model."""

import json
import threading
from typing import Any, Protocol

from .profiles import Profile
from .state import Snapshot, name_to_value


class DeviceError(Exception):
    """The robot answered with an error; `code` is its MIoT code when it gave one."""

    def __init__(self, message: str = "", code: int | None = None):
        super().__init__(message)
        self.code = code


class MiotLike(Protocol):
    def get_properties(self, properties, *, property_getter="get_prop", max_properties=None): ...
    def call_action_by(self, siid, aiid, params=None): ...
    def set_property_by(self, siid, piid, value, value_type=None): ...


class Vacuum:
    def __init__(self, dev: MiotLike, profile: Profile):
        self._dev = dev
        self._profile = profile
        self._lock = threading.Lock()

    @classmethod
    def connect(cls, ip: str, token: str, profile: Profile) -> "Vacuum":
        from miio import MiotDevice

        dev = MiotDevice(ip=ip, token=token, model=profile.models[0], mapping={}, timeout=5)
        # python-miio 0.5.12 reads Device.retry_count on every send(); the class default of 3
        # keeps a dead robot holding the device lock for ~20 s per call.
        dev.retry_count = 1
        return cls(dev, profile)

    def poll(self) -> Snapshot:
        props = [{"did": key, "siid": s, "piid": p} for key, (s, p) in self._profile.props.items()]
        with self._lock:
            results = self._dev.get_properties(
                props, property_getter="get_properties", max_properties=15
            )
        return self._profile.parse_properties(results)

    def start(self) -> None:
        self._action(*self._profile.actions["start"])

    def pause(self) -> None:
        self._action(*self._profile.actions["pause"])

    def stop(self) -> None:
        self._action(*self._profile.actions["stop"])

    def dock(self) -> None:
        self._action(*self._profile.actions["dock"])

    def locate(self) -> None:
        self._action(*self._profile.actions["locate"])

    def set_suction(self, name: str) -> None:
        self._set(*self._profile.props["suction"], name_to_value(self._profile.suction, name))

    def set_water(self, name: str) -> None:
        self._set(*self._profile.props["water"], name_to_value(self._profile.water, name))

    def clean_rooms(self, ids: list[int], suction: str, water: str) -> None:
        if not ids:
            raise DeviceError("no rooms selected")
        params = self._profile.room_clean_params(ids, suction, water)
        self._action(*self._profile.actions["clean"], params)

    def request_map_object(self) -> str:
        # The r2209 answers map-req (6.1) with an empty object name but publishes the current
        # map's cloud object in mult-map-info (6.8): {"object_name": "...", "md5": "..."}.
        info = self._profile.map_info_prop
        if info is not None:
            with self._lock:
                results = self._dev.get_properties(
                    [{"did": "map-info", "siid": info[0], "piid": info[1]}],
                    property_getter="get_properties",
                )
            for item in results or []:
                if item.get("code", 0) == 0 and (item.get("siid"), item.get("piid")) == info:
                    try:
                        name = json.loads(item.get("value") or "{}").get("object_name")
                    except (json.JSONDecodeError, AttributeError):
                        name = None
                    if name:
                        return str(name)
        name = self._map_request()
        if name is None:
            raise DeviceError("map request returned no object name")
        return name

    def request_live_map_object(self) -> str | None:
        """Ask for the current (live) map frame's cloud object; None when the robot names none.
        Never reads mult-map-info (6.8): that is the saved map, not the live one."""
        return self._map_request()

    def _map_request(self) -> str | None:
        result = self._action(
            *self._profile.actions["map_request"], [{"piid": 2, "value": '{"frame_type":"I"}'}]
        )
        out = result.get("out") or []
        for item in out:
            if isinstance(item, dict) and item.get("piid") == 3 and item.get("value"):
                return str(item["value"])
        # Some firmwares return bare values in the declared order [p1, p3, p5].
        if len(out) >= 2 and isinstance(out[1], str) and out[1]:
            return out[1]
        return None

    def update_map(self, payload: dict) -> None:
        siid, aiid = self._profile.actions["map_update"]
        value = json.dumps(payload, separators=(",", ":"))
        try:
            result = self._action(
                siid, aiid, [{"piid": self._profile.map_update_piid, "value": value}]
            )
        except DeviceError as exc:
            raise DeviceError(
                f"the robot refused the map change (code {exc.code})", code=exc.code
            ) from exc
        for item in result.get("out") or []:
            code = item.get("code", 0) if isinstance(item, dict) else 0
            if code:
                raise DeviceError(f"the robot refused the map change (code {code})", code=code)

    def set_room_settings_enabled(self, on: bool) -> None:
        self._set(*self._profile.props["room_settings"], 1 if on else 0)

    def _action(self, siid: int, aiid: int, params: list | None = None) -> dict[str, Any]:
        with self._lock:
            result = self._dev.call_action_by(siid, aiid, params or [])
        code = result.get("code", 0) if isinstance(result, dict) else 0
        if code != 0:
            raise DeviceError(f"action {siid}.{aiid} failed with code {code}", code=code)
        return result if isinstance(result, dict) else {}

    def _set(self, siid: int, piid: int, value: int) -> None:
        with self._lock:
            result = self._dev.set_property_by(siid, piid, value)
        items = result if isinstance(result, list) else [result]
        for item in items:
            code = item.get("code", 0) if isinstance(item, dict) else 0
            if code != 0:
                raise DeviceError(f"set {siid}.{piid} failed with code {code}", code=code)
