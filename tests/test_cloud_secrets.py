"""Password-equivalent data (hash, username, 2FA context) must not reach errors or logs."""

import logging

import pytest
import requests

from chippy.core.cloud import (
    CaptchaRequired,
    CloudUnreachable,
    TwoFactorRequired,
    XiaomiCloud,
)

USER = "secret.user@example.com"
LEAK = f"https://account.xiaomi.com/pass/serviceLoginAuth2?hash=ABCDEF&user={USER}"


class DeadSession:
    """Every request fails the way requests does: with the full URL in the message."""

    headers: dict = {}
    cookies: list = []

    def post(self, url, params=None, data=None, allow_redirects=True):
        raise requests.ConnectionError(f"Max retries exceeded with url: {LEAK}")

    def get(self, url, params=None, allow_redirects=True):
        raise requests.ConnectionError(f"Max retries exceeded with url: {LEAK}")


def _dead_cloud():
    cloud = XiaomiCloud(USER, "hunter2")
    cloud.session = DeadSession()
    cloud._login_step1 = lambda: "sign"  # step 1 answered; step 2's POST fails
    return cloud


def _assert_clean(text: str) -> None:
    assert "ABCDEF" not in text
    assert USER not in text
    assert "hunter2" not in text


def test_step2_network_error_is_clean_in_error_and_logs(caplog):
    caplog.set_level(logging.DEBUG)  # root logger; the app's micloud filter is not installed
    cloud = _dead_cloud()
    with pytest.raises(CloudUnreachable) as err:
        cloud.login()
    assert str(err.value) == "Couldn't reach Xiaomi's login service"
    assert err.value.__suppress_context__  # tracebacks don't show the requests error
    _assert_clean(str(err.value))
    _assert_clean(caplog.text)


def test_step2_network_error_direct_call_is_clean():
    with pytest.raises(CloudUnreachable) as err:
        _dead_cloud()._login_step2("sign")
    _assert_clean(str(err.value))


def test_email_code_network_errors_are_clean():
    cloud = _dead_cloud()
    with pytest.raises(CloudUnreachable):
        cloud.send_email_code("https://account.xiaomi.com/x?context=CTX")
    cloud._identity_context = "CTX"
    with pytest.raises(CloudUnreachable):
        cloud.verify_email_code("123456")
    with pytest.raises(CloudUnreachable):
        cloud.fetch_captcha("https://account.xiaomi.com/pass/getCode")


def test_challenge_messages_do_not_carry_the_url():
    url = "https://account.xiaomi.com/fe/service/identity/authStart?context=SECRETCTX"
    two_factor = TwoFactorRequired(url)
    assert url not in str(two_factor) and "SECRETCTX" not in str(two_factor)
    assert two_factor.url == url
    captcha = CaptchaRequired(url)
    assert "SECRETCTX" not in str(captcha) and captcha.url == url
