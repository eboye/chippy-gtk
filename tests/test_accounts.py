import pytest
from micloud.micloudexception import MiCloudAccessDenied

from chippy.core.accounts import credentials_for, discover_vacuums
from chippy.core.cloud import CaptchaRequired, TwoFactorRequired
from chippy.core.fake import FAKE_CAPTCHA, FAKE_EMAIL_CODE, FakeCloud


def test_discovery_lists_vacuums_supported_first():
    found = discover_vacuums(FakeCloud("a@b", "pw"))
    assert [(v.model, v.supported, v.tested) for v in found] == [
        ("dreame.vacuum.r2209", True, True),
        ("dreame.vacuum.r2228", True, False),
        ("roborock.vacuum.a15", False, False),
    ]
    assert all(v.country == "de" for v in found)


def test_discovery_survives_a_failing_region():
    class Flaky(FakeCloud):
        def get_devices(self, country=None):
            if country == "cn":
                raise RuntimeError("boom")
            return super().get_devices(country=country)

    assert len(discover_vacuums(Flaky("a@b", "pw"), countries=["cn", "de"])) == 3


def test_credentials_for():
    v = discover_vacuums(FakeCloud("a@b", "pw"))[0]
    creds = credentials_for(v, "a@b", "pw", {"user_id": "9"})
    assert (creds.model, creds.country, creds.username, creds.cloud_session) == (
        "dreame.vacuum.r2209",
        "de",
        "a@b",
        {"user_id": "9"},
    )
    assert creds.ip and creds.token


def test_fake_cloud_flows():
    cloud = FakeCloud("captcha-2fa@test", "pw")
    with pytest.raises(CaptchaRequired):
        cloud.login()
    assert cloud.fetch_captcha("fake://captcha").startswith(b"\x89PNG")
    cloud.captcha_code = FAKE_CAPTCHA
    with pytest.raises(TwoFactorRequired):
        cloud.login()
    cloud.send_email_code("fake://2fa")
    with pytest.raises(MiCloudAccessDenied):
        cloud.verify_email_code("000000")
    cloud.verify_email_code(FAKE_EMAIL_CODE)
    assert cloud.login() is True
    assert FakeCloud("wrong@test", "pw").login() is False


class HomesCloud(FakeCloud):
    """The old device list is empty (as for a shared robot); homes and shares hold them."""

    user_id = "500"

    def __init__(self, *a, fail=False):
        super().__init__(*a)
        self.fail = fail
        self.calls = []

    def get_devices(self, country=None):
        if self.fail:
            raise RuntimeError("service token missing")
        return []

    def request_country(self, path, country, params):
        import json

        data = json.loads(params["data"])
        self.calls.append((path, country, data))
        if self.fail:
            raise RuntimeError("service token missing")
        if country != "de":
            return json.dumps({"code": 0, "result": {}})
        if path == "/v2/homeroom/gethome":
            result = {"homelist": [{"id": "11", "uid": "500"}]}
        elif path == "/v2/user/get_device_cnt":
            result = {"share": {"share_family": [{"home_id": 22, "home_owner": 900}]}}
        elif path == "/v2/home/home_device_list":
            robot = {
                "did": "2002",
                "model": "dreame.vacuum.r2209",
                "name": "Shared",
                "localip": "127.0.0.9",
                "token": "3" * 32,
            }
            lamp = {"did": "2003", "model": "yeelink.light.color1", "name": "Lamp"}
            by_home = {11: [lamp], 22: [robot]}
            result = {"device_info": by_home.get(data["home_id"], [])}
        else:
            raise AssertionError(path)
        return json.dumps({"code": 0, "result": result})


def test_discovery_finds_a_robot_in_a_shared_home():
    cloud = HomesCloud("", "")
    found = discover_vacuums(cloud, countries=["de"])
    assert [(v.did, v.name, v.token) for v in found] == [("2002", "Shared", "3" * 32)]
    lists = [c for c in cloud.calls if c[0] == "/v2/home/home_device_list"]
    assert {(c[2]["home_id"], c[2]["home_owner"]) for c in lists} == {(11, 500), (22, 900)}


def test_discovery_lists_each_robot_once():
    class Both(HomesCloud):
        def get_devices(self, country=None):
            return (
                [
                    {
                        "did": "2002",
                        "model": "dreame.vacuum.r2209",
                        "name": "Shared",
                        "localip": "127.0.0.9",
                        "token": "3" * 32,
                    }
                ]
                if country == "de"
                else []
            )

    assert len(discover_vacuums(Both("", ""), countries=["de"])) == 1


def test_discovery_reports_why_when_every_request_fails():
    with pytest.raises(RuntimeError, match="service token missing"):
        discover_vacuums(HomesCloud("", "", fail=True), countries=["de", "us"])
