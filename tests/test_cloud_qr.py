"""QR login of XiaomiCloud against a scripted fake of Xiaomi's long-poll login."""

import json

import pytest
import requests
from requests.cookies import RequestsCookieJar

from chippy.core.cloud import (
    CloudError,
    CloudUnreachable,
    QrLogin,
    QrLoginCancelled,
    QrLoginExpired,
    XiaomiCloud,
)

HOST = "https://account.xiaomi.com"
QR_IMG = "https://account.xiaomi.com/qr.png?secret=QRSECRET"
POLL = "https://lp.account.xiaomi.com/lp/poll?k=POLLSECRET"
LOGIN_URL = "https://account.xiaomi.com/login?qr=1"
STS = "https://sts.api.io.mi.com/sts?d=STSSECRET"


class Resp:
    def __init__(self, body="", status=200, content=b"", cookies=None):
        self.text = body if isinstance(body, str) else "&&&START&&&" + json.dumps(body)
        self.status_code = status
        self.content = content
        self.cookies = cookies or RequestsCookieJar()


class FakeXiaomi:
    """`polls` scripts the long poll: each item is a status code, an exception, or 'ok'."""

    def __init__(self, polls=("ok",), start=None, sts_token="STOKEN"):
        self.polls = list(polls)
        self.start = start or {"qr": QR_IMG, "loginUrl": LOGIN_URL, "lp": POLL, "timeout": 300}
        self.sts_token = sts_token
        self.cookies = RequestsCookieJar()
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, **_kw):
        self.calls.append((url, params, timeout))
        if url == f"{HOST}/longPolling/loginUrl":
            return Resp(self.start)
        if url == QR_IMG:
            return Resp(content=b"\x89PNGqr")
        if url == POLL:
            item = self.polls.pop(0)
            if isinstance(item, BaseException):
                raise item
            if item == "ok":
                return Resp(
                    {
                        "userId": 4242,
                        "ssecurity": "SSEC",
                        "cUserId": "CUID",
                        "passToken": "PASSSECRET",
                        "location": STS,
                    }
                )
            return Resp("", status=item)
        if url == STS:
            jar = RequestsCookieJar()
            if self.sts_token:
                jar.set("serviceToken", self.sts_token)
            return Resp(cookies=jar)
        raise AssertionError(url)

    def close(self):
        pass


def _cloud(fake):
    cloud = XiaomiCloud("", "")
    cloud._init_session = lambda reset=False: setattr(cloud, "session", fake)
    return cloud


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_start_returns_image_link_and_poll_url():
    fake = FakeXiaomi()
    qr = _cloud(fake).start_qr_login()
    assert qr == QrLogin(b"\x89PNGqr", LOGIN_URL, POLL, 300)
    params = fake.calls[0][1]
    assert params["sid"] == "xiaomiio" and params["callback"] == "https://sts.api.io.mi.com/sts"


def test_start_without_qr_is_a_clean_error():
    fake = FakeXiaomi(start={"code": 70016, "desc": "nope", "lp": POLL})
    with pytest.raises(CloudError) as err:
        _cloud(fake).start_qr_login()
    assert "POLLSECRET" not in str(err.value)


def test_wait_retries_timeouts_then_signs_in():
    fake = FakeXiaomi(polls=[requests.Timeout("t " + POLL), 408, "ok"])
    cloud, clock = _cloud(fake), Clock()
    qr = cloud.start_qr_login()
    cloud.wait_qr_login(qr, lambda: False, clock=clock, sleep=clock.sleep)
    state = cloud.session_state()
    assert state == {"user_id": "4242", "service_token": "STOKEN", "ssecurity": "SSEC"}
    poll_timeouts = [t for url, _p, t in fake.calls if url == POLL]
    assert len(poll_timeouts) == 3 and all(t == (10, 10) for t in poll_timeouts)


def test_wait_expires_after_the_timeout():
    start = {"qr": QR_IMG, "loginUrl": LOGIN_URL, "lp": POLL, "timeout": 30}
    fake = FakeXiaomi(polls=[408] * 1000, start=start)
    cloud, clock = _cloud(fake), Clock()
    qr = cloud.start_qr_login()
    with pytest.raises(QrLoginExpired):
        cloud.wait_qr_login(qr, lambda: False, clock=clock, sleep=clock.sleep)
    assert clock.now >= 30


def test_wait_stops_when_cancelled():
    fake = FakeXiaomi(polls=[408, 408, "ok"])
    cloud, clock = _cloud(fake), Clock()
    qr = cloud.start_qr_login()
    asked = []

    def cancelled():
        asked.append(1)
        return len(asked) >= 2

    with pytest.raises(QrLoginCancelled):
        cloud.wait_qr_login(qr, cancelled, clock=clock, sleep=clock.sleep)
    assert cloud.service_token is None


def test_network_failure_is_unreachable_without_the_url():
    fake = FakeXiaomi(polls=[requests.ConnectionError("down " + POLL)])
    cloud, clock = _cloud(fake), Clock()
    qr = cloud.start_qr_login()
    with pytest.raises(CloudUnreachable) as err:
        cloud.wait_qr_login(qr, lambda: False, clock=clock, sleep=clock.sleep)
    assert "POLLSECRET" not in str(err.value) and err.value.__suppress_context__


def test_missing_service_token_is_an_error_without_secrets():
    fake = FakeXiaomi(sts_token=None)
    cloud, clock = _cloud(fake), Clock()
    qr = cloud.start_qr_login()
    with pytest.raises(CloudError) as err:
        cloud.wait_qr_login(qr, lambda: False, clock=clock, sleep=clock.sleep)
    text = str(err.value)
    assert "PASSSECRET" not in text and "STSSECRET" not in text and "SSEC" not in text
    assert cloud.service_token is None


def test_login_with_a_session_needs_no_password():
    cloud = XiaomiCloud("", "")
    cloud.restore_session({"user_id": "1", "service_token": "t", "ssecurity": "s"})
    assert cloud.login() is True
