"""CloudMap on the real XiaomiCloud with a scripted transport (no network): a session Xiaomi
refuses stays refused, and no fetch ever falls back to a password login."""

import json

import pytest
import requests
from requests.adapters import HTTPAdapter

from chippy.core import cloud as cloud_mod
from chippy.core.cloud import HTTP_TIMEOUT, CloudMap, CloudSessionExpired, XiaomiCloud

SESSION = {"user_id": "1", "service_token": "token", "ssecurity": "c2VjcmV0c2VjcmV0MTIzNA=="}
MAP_API = "https://de.api.io.mi.com/app/v2/home/get_interim_file_url"


class Creds:
    username, password, country = "someone@example.invalid", "pw", "de"
    cloud_session = SESSION


class Robot:
    def __init__(self):
        self.asked = 0

    def request_map_object(self):
        self.asked += 1
        return "obj/1"


@pytest.fixture
def net(monkeypatch):
    """micloud's request() scripted: "ok" answers a map URL; "403" does what micloud does on a
    403 (drops the service token; the undecryptable body yields None)."""
    log = {"requests": [], "logins": 0, "downloads": 0, "answer": "ok"}

    def fake_request(self, url, params):
        log["requests"].append((url, json.loads(params["data"])))
        if log["answer"] == "403":
            self.service_token = None
            return None
        return json.dumps({"code": 0, "result": {"url": "https://files.example/x"}})

    def fake_login_request(self):
        log["logins"] += 1
        raise cloud_mod.TwoFactorRequired("https://account.example.invalid/2fa")

    class Download:
        content = b"map"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout):
        log["downloads"] += 1
        return Download()

    monkeypatch.setattr(XiaomiCloud, "request", fake_request)
    monkeypatch.setattr(XiaomiCloud, "_login_request", fake_login_request)
    monkeypatch.setattr(requests, "get", fake_get)
    return log


def test_valid_session_fetches_with_one_api_request_and_no_login(net):
    fetcher = CloudMap(Creds())
    assert fetcher.fetch_object_payload("obj/1") == b"map"
    assert fetcher.fetch_object_payload("obj/1") == b"map"
    assert net["requests"] == [(MAP_API, {"obj_name": "obj/1"})] * 2
    assert net["logins"] == 0 and net["downloads"] == 2
    assert not fetcher.expired


def test_refused_session_expires_for_good(net):
    fetcher = CloudMap(Creds())
    assert fetcher.fetch_object_payload("obj/1") == b"map"
    net["answer"] = "403"
    with pytest.raises(CloudSessionExpired, match="sign in again"):
        fetcher.fetch_object_payload("obj/1")
    assert fetcher.expired and len(net["requests"]) == 2
    robot = Robot()
    for _ in range(4):  # later live ticks / map refreshes
        with pytest.raises(CloudSessionExpired):
            fetcher.fetch_object_payload("obj/1")
        with pytest.raises(CloudSessionExpired):
            fetcher.fetch_payload(robot)
    assert len(net["requests"]) == 2  # zero network calls after expiry
    assert net["logins"] == 0 and net["downloads"] == 1
    assert robot.asked == 0  # not even the robot is asked for a map it can't download

    net["answer"] = "ok"
    assert CloudMap(Creds()).fetch_object_payload("obj/1") == b"map"  # a new sign-in works


@pytest.mark.parametrize("missing", ["service_token", "user_id", "ssecurity"])
def test_incomplete_saved_session_is_expired_without_network(net, missing):
    class Partial(Creds):
        cloud_session = {**SESSION, missing: ""}

    with pytest.raises(CloudSessionExpired):
        CloudMap(Partial()).fetch_object_payload("obj/1")
    assert net["requests"] == [] and net["logins"] == 0 and net["downloads"] == 0


def test_real_micloud_403_expires_the_map(monkeypatch):
    """Through micloud's own request(): a 403 from the API makes CloudMap expire."""
    seen = []

    def send(self, request, **kwargs):
        seen.append((request.url, kwargs.get("timeout")))
        response = requests.Response()
        response.status_code, response._content, response.url = 403, b"", request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr(XiaomiCloud, "_login_request", lambda self: pytest.fail("login"))
    fetcher = CloudMap(Creds())
    with pytest.raises(CloudSessionExpired):
        fetcher.fetch_object_payload("obj/1")
    with pytest.raises(CloudSessionExpired):
        fetcher.fetch_object_payload("obj/1")
    assert seen == [(MAP_API, HTTP_TIMEOUT)]


def test_every_cloud_session_request_has_a_timeout(monkeypatch):
    seen = []

    def send(self, request, **kwargs):
        seen.append(kwargs.get("timeout"))
        response = requests.Response()
        response.status_code, response._content = 200, b"{}"
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    cloud = XiaomiCloud("someone@example.invalid", "pw")
    cloud._init_session()
    cloud.session.get("https://account.example.invalid/a")
    cloud.session.post("https://account.example.invalid/b", data={"x": "1"})
    cloud.session.get("https://account.example.invalid/c", timeout=3)
    cloud._init_session(reset=True)  # micloud's request() resets the session every call
    cloud.session.get("https://account.example.invalid/d")
    assert seen == [HTTP_TIMEOUT, HTTP_TIMEOUT, 3, HTTP_TIMEOUT]


def test_hung_login_step_times_out_as_unreachable(monkeypatch):
    def send(self, request, **kwargs):
        assert kwargs.get("timeout") == HTTP_TIMEOUT
        raise requests.ReadTimeout("read timed out")

    monkeypatch.setattr(HTTPAdapter, "send", send)
    cloud = XiaomiCloud("someone@example.invalid", "pw")
    with pytest.raises(cloud_mod.CloudUnreachable):
        cloud.login()
