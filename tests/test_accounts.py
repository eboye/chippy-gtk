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
