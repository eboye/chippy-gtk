import json

import pytest
from micloud.micloudexception import MiCloudAccessDenied

from chippy.core.cloud import CloudError, TwoFactorRequired, XiaomiCloud


class FakeResponse:
    def __init__(self, payload):
        self.text = "&&&START&&&" + json.dumps(payload)
        self.status_code = 200


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def post(self, url, params=None, data=None, allow_redirects=True):
        self.sent = {"url": url, "params": params, "data": data}
        return FakeResponse(self.payload)


def _cloud(payload):
    cloud = XiaomiCloud("me@example.com", "pw")
    cloud.session = FakeSession(payload)
    return cloud


def test_step2_two_factor():
    cloud = _cloud(
        {"result": "ok", "notificationUrl": "/fe/service/identity/authStart?x=1", "location": ""}
    )
    with pytest.raises(TwoFactorRequired) as err:
        cloud._login_step2("sign")
    assert err.value.url == "https://account.xiaomi.com/fe/service/identity/authStart?x=1"
    assert isinstance(err.value, MiCloudAccessDenied)


def test_step2_two_factor_even_with_a_location():
    cloud = _cloud(
        {
            "result": "ok",
            "code": 0,
            "location": "https://account.xiaomi.com/x",
            "notificationUrl": "https://account.xiaomi.com/fe/service/identity/authStart?c=1",
            "securityStatus": 16,
        }
    )
    with pytest.raises(TwoFactorRequired):
        cloud._login_step2("sign")


def test_step2_bad_password_reports_xiaomis_code():
    with pytest.raises(MiCloudAccessDenied, match=r"rejected the login \(code 70016, bad pw"):
        _cloud({"result": "error", "code": 70016, "desc": "bad pw"})._login_step2("sign")


def test_step2_captcha():
    from chippy.core.cloud import CaptchaRequired

    with pytest.raises(CaptchaRequired) as err:
        _cloud({"code": 87001, "captchaUrl": "/pass/getCode?x"})._login_step2("sign")
    assert err.value.url == "https://account.xiaomi.com/pass/getCode?x"


def test_step2_sends_the_captcha_answer_once():
    cloud = _cloud({"code": 87001, "captchaUrl": "/pass/getCode?x"})
    cloud.captcha_code = "ab12"
    with pytest.raises(MiCloudAccessDenied):
        cloud._login_step2("sign")
    assert cloud.session.sent["params"]["captCode"] == "ab12"
    assert cloud.captcha_code is None


def test_existing_session_keeps_its_cookies():
    cloud = XiaomiCloud("me@example.com", "pw")
    cloud._init_session()
    cloud.session.cookies.set("ick", "captcha-cookie")
    cloud._init_session()  # micloud calls this again at the start of every login
    assert cloud.session.cookies.get("ick") == "captcha-cookie"


def test_step2_ok_sends_query_params():
    cloud = _cloud(
        {
            "result": "ok",
            "location": "https://sts/x",
            "userId": 1,
            "ssecurity": "SSECURITY",
            "cUserId": "c",
            "passToken": "p",
            "code": 0,
        }
    )
    assert cloud._login_step2("sign") == "https://sts/x"
    assert cloud.user_id == 1 and cloud.ssecurity == "SSECURITY"
    sent = cloud.session.sent
    assert sent["data"] is None and sent["params"]["user"] == "me@example.com"
    assert sent["params"]["_sign"] == "sign" and "pw" not in sent["params"].values()


def test_session_identity_matches_current_xiaomi_apps():
    cloud = XiaomiCloud("me@example.com", "pw")
    cloud._init_session()
    assert cloud.session.headers["User-Agent"].endswith(" APP/com.xiaomi.mihome APPV/10.5.201")
    versions = {c.value for c in cloud.session.cookies if c.name == "sdkVersion"}
    assert versions == {"accountsdk-18.8.15"}
    assert len({c.value for c in cloud.session.cookies if c.name == "deviceId"}) == 1


def test_map_file_url(monkeypatch):
    cloud = XiaomiCloud("u", "p")
    seen = {}

    def fake_request_country(part, country, params):
        seen.update(part=part, country=country, data=json.loads(params["data"]))
        return json.dumps({"code": 0, "result": {"url": "https://files/x"}})

    monkeypatch.setattr(cloud, "request_country", fake_request_country)
    assert cloud.map_file_url("obj/1", "de") == "https://files/x"
    assert seen == {
        "part": "/v2/home/get_interim_file_url",
        "country": "de",
        "data": {"obj_name": "obj/1"},
    }


def test_property_history(monkeypatch):
    cloud = XiaomiCloud("u", "p")
    seen = {}

    def fake_request_country(part, country, params):
        seen.update(part=part, country=country, data=json.loads(params["data"]))
        rows = [{"time": 20, "value": "b"}, {"time": 10, "value": "a"}, {"time": "x"}, 5]
        return json.dumps({"code": 0, "result": rows})

    monkeypatch.setattr(cloud, "request_country", fake_request_country)
    assert cloud.property_history("42", "6.1", 1, 99, 50, "de") == [(10, "a"), (20, "b")]
    assert seen == {
        "part": "/user/get_user_device_data",
        "country": "de",
        "data": {
            "did": "42",
            "key": "6.1",
            "type": "prop",
            "time_start": 1,
            "time_end": 99,
            "limit": 50,
        },
    }


def test_property_history_failure_never_echoes_the_reply(monkeypatch):
    cloud = XiaomiCloud("u", "p")
    monkeypatch.setattr(cloud, "request_country", lambda *a: '{"code": 3, "secret": "s3cr3t"}')
    with pytest.raises(CloudError) as err:
        cloud.property_history("42", "6.1", 1, 99, 50, "de")
    assert "s3cr3t" not in str(err.value) and "code 3" in str(err.value)


def test_map_file_url_failure(monkeypatch):
    cloud = XiaomiCloud("u", "p")
    monkeypatch.setattr(cloud, "request_country", lambda *a: None)
    with pytest.raises(CloudError):
        cloud.map_file_url("obj/1", "de")


class _Creds:
    username, password, country = "me@example.com", "pw", "de"


class _MapVacuum:
    def __init__(self):
        self.requests = 0

    def request_map_object(self):
        self.requests += 1
        return "obj/1"


def _cloud_map(monkeypatch, url_results):
    """CloudMap with a fake XiaomiCloud whose map_file_url replays `url_results`."""
    from chippy.core import cloud as cloud_mod

    record = {"instances": 0, "logins": 0}

    class FakeCloud:
        def __init__(self, username, password):
            record["instances"] += 1
            self.user_id = self.service_token = None

        def restore_session(self, state):
            record["restored"] = state
            self.user_id, self.service_token = "1", "token"

        def login(self):
            record["logins"] += 1  # CloudMap must never call it
            return True

        def property_history(self, did, key, start, end, limit, country):
            record["history"] = (did, key, start, end, limit, country)
            self.service_token = None  # what micloud does when Xiaomi answers 403
            raise CloudError("refused")

        def map_file_url(self, object_name, country):
            assert (object_name, country) == ("obj/1", "de")
            result = url_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    class FakeResponse:
        content = b"map"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(cloud_mod, "XiaomiCloud", FakeCloud)
    monkeypatch.setattr("requests.get", lambda url, timeout: FakeResponse())
    return cloud_mod.CloudMap(_Creds()), record


def test_cloud_map_reuses_one_restored_session(monkeypatch):
    fetcher, record = _cloud_map(monkeypatch, ["https://files/1", "https://files/2"])
    vac = _MapVacuum()
    assert fetcher.fetch_payload(vac) == b"map"
    assert fetcher.fetch_payload(vac) == b"map"
    assert record["instances"] == 1
    assert record["logins"] == 0  # the saved session is used as is, never a login


def test_cloud_map_error_keeps_a_valid_session_without_a_retry(monkeypatch):
    from micloud.micloudexception import MiCloudException

    fetcher, record = _cloud_map(
        monkeypatch, [MiCloudException("decrypt failed"), CloudError("no map"), "https://f/1"]
    )
    with pytest.raises(MiCloudException):
        fetcher.fetch_payload(_MapVacuum())
    with pytest.raises(CloudError):
        fetcher.fetch_payload(_MapVacuum())
    assert fetcher.fetch_payload(_MapVacuum()) == b"map"  # the token survived: not expired
    assert record["instances"] == 1 and record["logins"] == 0


def test_cloud_map_history_refused_expires_the_session(monkeypatch):
    from chippy.core.cloud import CloudSessionExpired

    fetcher, record = _cloud_map(monkeypatch, ["https://files/1"])
    with pytest.raises(CloudSessionExpired):
        fetcher.property_history("42", "6.1", 1, 99, 50)
    assert record["history"] == ("42", "6.1", 1, 99, 50, "de")
    assert fetcher.expired
    with pytest.raises(CloudSessionExpired):
        fetcher.fetch_payload(_MapVacuum())
    assert record["logins"] == 0


def test_cloud_map_fetches_a_named_object_without_asking_the_robot(monkeypatch):
    fetcher, record = _cloud_map(monkeypatch, ["https://files/1", "https://files/2"])
    assert fetcher.fetch_object_payload("obj/1") == b"map"
    vac = _MapVacuum()
    assert fetcher.fetch_payload(vac) == b"map"
    assert vac.requests == 1 and record["instances"] == 1


def test_cloud_map_object_fetch_needs_a_stored_login():
    from chippy.core.cloud import CloudMap

    class NoLogin(_Creds):
        password = ""

    with pytest.raises(CloudError, match="sign in"):
        CloudMap(NoLogin()).fetch_object_payload("obj/1")


def test_cloud_map_needs_a_stored_login():
    from chippy.core.cloud import CloudMap

    class NoLogin(_Creds):
        username = ""

    with pytest.raises(CloudError, match="sign in"):
        CloudMap(NoLogin()).fetch_payload(_MapVacuum())


_SIGNED = "https://files.example/obj?AWSAccessKeyId=AKIAKEY&Signature=secret123"


def _forbidden(url, timeout):
    import requests

    response = requests.Response()
    response.status_code, response.reason, response.url = 403, "Forbidden", url
    return response


def _unreachable(url, timeout):
    import requests

    raise requests.ConnectionError(f"Max retries exceeded with url: {url}")


@pytest.mark.parametrize(
    ("get", "summary"),
    [(_forbidden, "map download failed: HTTP 403"), (_unreachable, "ConnectionError")],
)
def test_map_download_errors_never_carry_the_signed_url(monkeypatch, get, summary):
    fetcher, _ = _cloud_map(monkeypatch, [_SIGNED])
    monkeypatch.setattr("requests.get", get)
    with pytest.raises(CloudError) as caught:
        fetcher.fetch_object_payload("obj/1")
    assert summary in str(caught.value)
    assert "secret123" not in str(caught.value) and "AKIAKEY" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_live_loop_warning_has_no_signed_url(monkeypatch, tmp_path, caplog):
    from chippy.controller import VacuumController
    from chippy.core.fake import FakeVacuum, build_fake_payload
    from chippy.core.profiles import R2209
    from chippy.store import MapCache, RoomStore
    from tests.test_controller import InlineRunner, ManualScheduler

    fetcher, _ = _cloud_map(monkeypatch, [_SIGNED])
    monkeypatch.setattr("requests.get", _forbidden)
    sched = ManualScheduler()
    ctl = VacuumController(
        FakeVacuum(),
        R2209,
        RoomStore(tmp_path / "rooms.json", legacy=tmp_path / "none.json"),
        MapCache(tmp_path / "map.payload"),
        build_fake_payload,
        runner=InlineRunner(),
        scheduler=sched,
        fetch_live_payload=lambda: fetcher.fetch_object_payload("obj/1"),
    )
    ctl.start()
    ctl.act("start")
    with caplog.at_level("DEBUG"):
        sched.fire_named("live")
    assert "HTTP 403" in caplog.text and "secret123" not in caplog.text


def test_map_file_url_error_does_not_echo_the_reply(monkeypatch):
    cloud = XiaomiCloud("u", "p")
    reply = json.dumps({"code": 7, "result": {"link": _SIGNED}})
    monkeypatch.setattr(cloud, "request_country", lambda *a: reply)
    with pytest.raises(CloudError) as caught:
        cloud.map_file_url("obj/1", "de")
    assert "code 7" in str(caught.value) and "secret123" not in str(caught.value)
    monkeypatch.setattr(cloud, "request_country", lambda *a: "not json " + _SIGNED)
    with pytest.raises(CloudError) as caught:
        cloud.map_file_url("obj/1", "de")
    assert "secret123" not in str(caught.value)


def test_cloud_map_with_a_qr_session_and_no_password(monkeypatch):
    fetcher, record = _cloud_map(monkeypatch, ["https://files/1"])

    class QrCreds(_Creds):
        username = password = ""
        cloud_session = {"user_id": "4242", "service_token": "t", "ssecurity": "s"}

    fetcher._creds = QrCreds()
    assert fetcher.fetch_object_payload("obj/1") == b"map"
    assert record["logins"] == 0


def test_cloud_map_with_neither_session_nor_password_asks_to_sign_in():
    from chippy.core.cloud import CloudMap

    class Nothing(_Creds):
        username = password = ""
        cloud_session = {}

    with pytest.raises(CloudError, match="sign in again"):
        CloudMap(Nothing()).fetch_object_payload("obj/1")
