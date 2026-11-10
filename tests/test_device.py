import json

import pytest

from chippy.core.device import DeviceError, Vacuum
from chippy.core.profiles import R2209


class RecordingDev:
    def __init__(self, props=None, action_result=None, set_result=None):
        self.calls = []
        self._props = props or []
        self._action_result = action_result
        self._set_result = set_result

    def get_properties(self, properties, *, property_getter="get_prop", max_properties=None):
        self.calls.append(("get", properties, property_getter, max_properties))
        return self._props

    def call_action_by(self, siid, aiid, params=None):
        self.calls.append(("action", siid, aiid, params))
        if self._action_result is not None:
            return self._action_result
        return {"code": 0, "siid": siid, "aiid": aiid, "out": []}

    def set_property_by(self, siid, piid, value, value_type=None):
        self.calls.append(("set", siid, piid, value))
        return self._set_result or [{"code": 0, "siid": siid, "piid": piid}]


def test_poll_requests_all_props_in_one_batch():
    dev = RecordingDev(props=[{"siid": 3, "piid": 1, "code": 0, "value": 55}])
    snap = Vacuum(dev, R2209).poll()
    kind, props, getter, max_props = dev.calls[0]
    assert kind == "get" and getter == "get_properties" and max_props == 15
    assert {(p["siid"], p["piid"]) for p in props} == {
        (2, 1),
        (2, 2),
        (3, 1),
        (3, 2),
        (4, 2),
        (4, 3),
        (4, 4),
        (4, 5),
        (4, 6),
        (4, 26),
    }
    assert snap.battery == 55


@pytest.mark.parametrize(
    ("method", "siid", "aiid"),
    [("start", 2, 1), ("pause", 2, 2), ("stop", 4, 2), ("dock", 3, 1), ("locate", 7, 1)],
)
def test_simple_actions(method, siid, aiid):
    dev = RecordingDev()
    getattr(Vacuum(dev, R2209), method)()
    assert dev.calls == [("action", siid, aiid, [])]


def test_action_error_code_raises():
    dev = RecordingDev(action_result={"code": -9999})
    with pytest.raises(DeviceError, match="action 2.1 failed with code -9999"):
        Vacuum(dev, R2209).start()


def test_set_suction_and_water():
    dev = RecordingDev()
    vac = Vacuum(dev, R2209)
    vac.set_suction("turbo")
    vac.set_water("low")
    assert dev.calls == [("set", 4, 4, 3), ("set", 4, 5, 1)]


def test_set_error_code_raises():
    dev = RecordingDev(set_result=[{"code": -4004}])
    with pytest.raises(DeviceError, match="set 4.4 failed with code -4004") as caught:
        Vacuum(dev, R2209).set_suction("quiet")
    assert caught.value.code == -4004


def test_clean_rooms_payload():
    dev = RecordingDev()
    Vacuum(dev, R2209).clean_rooms([16, 18], "strong", "high")
    kind, siid, aiid, params = dev.calls[0]
    assert (kind, siid, aiid) == ("action", 4, 1)
    assert params[0] == {"piid": 1, "value": 18}
    assert params[1]["piid"] == 10
    assert json.loads(params[1]["value"]) == {"selects": [[16, 1, 2, 3, 1], [18, 1, 2, 3, 2]]}


def test_clean_rooms_requires_ids():
    with pytest.raises(DeviceError, match="no rooms selected"):
        Vacuum(RecordingDev(), R2209).clean_rooms([], "standard", "medium")


def test_request_map_object_dict_out():
    dev = RecordingDev(
        action_result={"code": 0, "out": [{"piid": 1, "value": ""}, {"piid": 3, "value": "obj/1"}]}
    )
    assert Vacuum(dev, R2209).request_map_object() == "obj/1"
    assert dev.calls[-1] == ("action", 6, 1, [{"piid": 2, "value": '{"frame_type":"I"}'}])


def test_request_map_object_prefers_mult_map_info():
    # Shaped like the real r2209 reply (its map-req returns an empty name); ids are made up.
    info = '{"object_name":"10000001/2000000002/9","md5":"0123456789abcdef0123456789abcdef"}'
    dev = RecordingDev(props=[{"siid": 6, "piid": 8, "code": 0, "value": info}])
    assert Vacuum(dev, R2209).request_map_object() == "10000001/2000000002/9"
    assert not [c for c in dev.calls if c[0] == "action"]


def test_request_map_object_plain_out():
    dev = RecordingDev(action_result={"code": 0, "out": ["", "obj/2", 1700000000]})
    assert Vacuum(dev, R2209).request_map_object() == "obj/2"


def test_request_map_object_missing():
    dev = RecordingDev(action_result={"code": 0, "out": []})
    with pytest.raises(DeviceError, match="no object name"):
        Vacuum(dev, R2209).request_map_object()


def test_request_live_map_object_never_reads_map_info():
    info = '{"object_name":"saved/1","md5":"x"}'
    dev = RecordingDev(
        props=[{"siid": 6, "piid": 8, "code": 0, "value": info}],
        action_result={
            "code": 0,
            "out": [{"piid": 1, "value": ""}, {"piid": 3, "value": "live/1"}],
        },
    )
    assert Vacuum(dev, R2209).request_live_map_object() == "live/1"
    assert dev.calls == [("action", 6, 1, [{"piid": 2, "value": '{"frame_type":"I"}'}])]


def test_request_live_map_object_plain_out_and_empty():
    dev = RecordingDev(action_result={"code": 0, "out": ["", "live/2", 1700000000]})
    assert Vacuum(dev, R2209).request_live_map_object() == "live/2"
    for out in ([], ["", ""], [{"piid": 1, "value": ""}, {"piid": 3, "value": ""}]):
        dev = RecordingDev(action_result={"code": 0, "out": out})
        assert Vacuum(dev, R2209).request_live_map_object() is None


def test_device_calls_are_serialised():
    import threading
    import time

    class SlowDev(RecordingDev):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.guard = threading.Lock()

        def _slow(self):
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1

        def get_properties(self, *args, **kwargs):
            self._slow()
            return []

        def call_action_by(self, siid, aiid, params=None):
            self._slow()
            return {"code": 0}

    dev = SlowDev()
    vac = Vacuum(dev, R2209)
    threads = [threading.Thread(target=vac.poll), threading.Thread(target=vac.locate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert dev.max_active == 1


def test_connect_uses_short_timeout_and_one_retry():
    vac = Vacuum.connect("192.0.2.1", "0" * 32, R2209)
    dev = vac._dev
    assert dev._protocol._timeout == 5
    assert dev.retry_count == 1


def test_update_map_sends_compact_json_on_6_2():
    dev = RecordingDev()
    Vacuum(dev, R2209).update_map({"vw": {"line": [], "rect": [[0, 0, 500, 500]], "mop": []}})
    assert dev.calls[-1] == (
        "action",
        6,
        2,
        [{"piid": 4, "value": '{"vw":{"line":[],"rect":[[0,0,500,500]],"mop":[]}}'}],
    )


def test_update_map_refusal_is_a_device_error():
    dev = RecordingDev(action_result={"code": -1})
    with pytest.raises(DeviceError, match=r"refused the map change \(code -1\)") as caught:
        Vacuum(dev, R2209).update_map({"cleanOrder": [1]})
    assert caught.value.code == -1


def test_update_map_refusal_inside_out_is_a_device_error():
    dev = RecordingDev(action_result={"code": 0, "out": [{"piid": 4, "code": -5}]})
    with pytest.raises(DeviceError, match=r"refused the map change \(code -5\)") as caught:
        Vacuum(dev, R2209).update_map({"cleanOrder": [1]})
    assert caught.value.code == -5


def test_room_settings_switch_is_4_26():
    dev = RecordingDev()
    vac = Vacuum(dev, R2209)
    vac.set_room_settings_enabled(True)
    vac.set_room_settings_enabled(False)
    assert dev.calls == [("set", 4, 26, 1), ("set", 4, 26, 0)]
