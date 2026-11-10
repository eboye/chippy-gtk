"""Email 2FA flow of XiaomiCloud against a scripted fake of Xiaomi's identity pages."""

import json

import pytest
from micloud.micloudexception import MiCloudAccessDenied
from requests.cookies import RequestsCookieJar

from chippy.core.cloud import CloudError, XiaomiCloud

HOST = "https://account.xiaomi.com"
NOTIFY = f"{HOST}/fe/service/identity/authStart?sid=xiaomiio&context=CTX1"
CHECK = f"{HOST}/identity/result/check?sid=xiaomiio&context=CTX1"
END = f"{HOST}/pass/serviceLoginAuth2/end?x=1"
STS = "https://sts.api.io.mi.com/sts?d=1"


class Resp:
    def __init__(self, body="", headers=None, status=200, cookies=None):
        self.text = body if isinstance(body, str) else "&&&START&&&" + json.dumps(body)
        self.headers = headers or {}
        self.status_code = status
        self.cookies = cookies or RequestsCookieJar()


class FakeXiaomi:
    def __init__(self, verify_reply=None):
        self.cookies = RequestsCookieJar()
        self.cookies.set("ick", "ICK", domain=".account.xiaomi.com")
        self.cookies.set("userId", "me@example.com")  # micloud sets the login name here
        self.calls = []
        self.verify_reply = verify_reply or {"code": 0, "location": CHECK}

    def get(self, url, params=None, allow_redirects=True):
        self.calls.append(("GET", url.split("?")[0], params))
        if url == CHECK:
            return Resp(status=302, headers={"Location": END})
        if url == END:
            pragma = json.dumps({"ssecurity": "SSEC", "psecurity": "P"})
            return Resp(status=302, headers={"extension-pragma": pragma, "Location": STS})
        if url == STS:
            self.cookies.set("serviceToken", "STOKEN", domain=".sts.api.io.mi.com")
            self.cookies.set("userId", "123456", domain=".sts.api.io.mi.com")
            self.cookies.set("cUserId", "CUID", domain=".sts.api.io.mi.com")
            return Resp()
        return Resp("{}")

    def post(self, url, params=None, data=None):
        self.calls.append(("POST", url, params, data))
        if url.endswith("/sendEmailTicket"):
            return Resp({"code": 0})
        if url.endswith("/verifyEmail"):
            return Resp(self.verify_reply)
        raise AssertionError(url)


def _cloud(fake):
    cloud = XiaomiCloud("me@example.com", "pw")
    cloud.session = fake
    return cloud


def test_send_and_verify_email_code_logs_in():
    fake = FakeXiaomi()
    cloud = _cloud(fake)
    cloud.send_email_code(NOTIFY)
    send = next(c for c in fake.calls if c[0] == "POST")
    assert send[2]["context"] == "CTX1" and send[3]["ick"] == "ICK"

    cloud.verify_email_code(" 123456 ")
    verify = [c for c in fake.calls if c[0] == "POST"][-1]
    assert verify[1] == f"{HOST}/identity/auth/verifyEmail"
    assert verify[3]["ticket"] == "123456" and verify[3]["_flag"] == "8"
    assert cloud.session_state() == {
        "user_id": "123456",
        "service_token": "STOKEN",
        "ssecurity": "SSEC",
    }


def test_wrong_code_is_access_denied():
    fake = FakeXiaomi(verify_reply={"code": 70014, "desc": "Wrong code"})
    cloud = _cloud(fake)
    cloud.send_email_code(NOTIFY)
    with pytest.raises(MiCloudAccessDenied, match="Wrong code"):
        cloud.verify_email_code("000000")


def test_verify_before_send_is_an_error():
    with pytest.raises(CloudError, match="send_email_code"):
        _cloud(FakeXiaomi()).verify_email_code("1")


def test_notification_url_without_context():
    with pytest.raises(CloudError, match="context"):
        _cloud(FakeXiaomi()).send_email_code(f"{HOST}/fe/service/identity/authStart?sid=x")


def test_restore_session_skips_password_login():
    cloud = XiaomiCloud("me@example.com", "pw")
    cloud.restore_session({"user_id": "1", "service_token": "t", "ssecurity": "s"})
    assert cloud.login() is True  # micloud returns early when a token is held
    empty = XiaomiCloud("me@example.com", "pw")
    empty.restore_session({})
    assert empty.service_token is None
