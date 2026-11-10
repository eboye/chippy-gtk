import pytest
import requests

from chippy.core.cloud import CloudUnreachable
from chippy.core.fake import FAKE_CAPTCHA, FAKE_EMAIL_CODE, FakeCloud
from chippy.ui.login import LoginFlow
from tests.test_controller import InlineRunner


def run_flow(username):
    pages = []
    flow = LoginFlow(FakeCloud, InlineRunner(), lambda page, data: pages.append((page, data)))
    flow.sign_in(username, "pw")
    return flow, pages


def test_plain_login_lists_devices():
    _, pages = run_flow("plain@test")
    assert pages[-1][0] == "devices"
    assert [v.model for v in pages[-1][1]["vacuums"]][0] == "dreame.vacuum.r2209"


def test_captcha_then_code_then_choose():
    flow, pages = run_flow("captcha-2fa@test")
    assert pages[-1][0] == "captcha" and pages[-1][1]["image"].startswith(b"\x89PNG")
    flow.submit_captcha("nope")
    assert pages[-1][0] == "captcha"  # wrong captcha: asked again
    flow.submit_captcha(FAKE_CAPTCHA)
    assert pages[-1][0] == "code"
    flow.submit_code("000000")
    assert pages[-1] == ("error", {"page": "code", "message": "Wrong code"})
    flow.submit_code(FAKE_EMAIL_CODE)
    assert pages[-1][0] == "devices"
    flow.choose(pages[-1][1]["vacuums"][0])
    page, data = pages[-1]
    assert page == "done" and data["credentials"].username == "captcha-2fa@test"


def test_wrong_password():
    _, pages = run_flow("wrong@test")
    assert pages[-1][0] == "error" and pages[-1][1]["page"] == "account"


class DeferredRunner:
    """Holds jobs until `finish()`, like ThreadRunner while a request is in flight."""

    def __init__(self):
        self.pending = []

    def run(self, func, callback):
        self.pending.append((func, callback))

    def finish(self):
        while self.pending:
            func, callback = self.pending.pop(0)
            InlineRunner().run(func, callback)


class CountingCloud(FakeCloud):
    verify_calls = 0
    login_calls = 0

    def verify_email_code(self, code):
        CountingCloud.verify_calls += 1
        return super().verify_email_code(code)

    def login(self):
        CountingCloud.login_calls += 1
        return super().login()


def _at_code_page():
    CountingCloud.verify_calls = CountingCloud.login_calls = 0
    pages, runner = [], DeferredRunner()
    flow = LoginFlow(CountingCloud, runner, lambda page, data: pages.append((page, data)))
    flow.sign_in("2fa@test", "pw")
    runner.finish()
    assert pages[-1][0] == "code" and not flow.busy
    return flow, pages, runner


def test_second_code_submission_while_pending_is_ignored():
    flow, pages, runner = _at_code_page()
    flow.submit_code("000000")
    assert flow.busy
    flow.submit_code("000000")  # a second Return while the first request runs
    flow.resend_code()
    runner.finish()
    assert CountingCloud.verify_calls == 1
    assert [p for p, _ in pages].count("error") == 1
    assert not flow.busy


def test_second_sign_in_and_captcha_while_pending_are_ignored():
    pages, runner = [], DeferredRunner()
    flow = LoginFlow(CountingCloud, runner, lambda page, data: pages.append((page, data)))
    CountingCloud.login_calls = 0
    flow.sign_in("captcha@test", "pw")
    flow.sign_in("captcha@test", "pw")
    runner.finish()
    assert CountingCloud.login_calls == 1 and pages[-1][0] == "captcha"
    flow.submit_captcha("a")
    flow.submit_captcha("b")
    runner.finish()
    assert CountingCloud.login_calls == 2


def test_three_wrong_codes_lock_verify_until_a_new_code():
    flow, pages, runner = _at_code_page()
    for _ in range(3):
        flow.submit_code("000000")
        runner.finish()
    assert flow.code_locked
    assert pages[-1][0] == "error" and "new code" in pages[-1][1]["message"]
    count = len(pages)
    flow.submit_code(FAKE_EMAIL_CODE)  # blocked: nothing is sent
    runner.finish()
    assert CountingCloud.verify_calls == 3 and len(pages) == count
    flow.resend_code()
    runner.finish()
    assert not flow.code_locked and pages[-1] == ("code", {"resent": True})
    flow.submit_code(FAKE_EMAIL_CODE)
    runner.finish()
    assert pages[-1][0] == "devices"


class OfflineCloud(FakeCloud):
    def login(self):
        raise CloudUnreachable()


class RawOfflineCloud(FakeCloud):
    def login(self):
        raise requests.ConnectionError("https://account.xiaomi.com/x?hash=ABCDEF")


@pytest.mark.parametrize("cloud", [OfflineCloud, RawOfflineCloud])
def test_network_failure_is_reported_as_such(cloud):
    pages = []
    flow = LoginFlow(cloud, InlineRunner(), lambda page, data: pages.append((page, data)))
    flow.sign_in("plain@test", "pw")
    assert pages[-1] == (
        "error",
        {"page": "account", "message": "Couldn't reach Xiaomi. Check your connection."},
    )


def test_captcha_gives_up_after_three_tries():
    flow, pages = run_flow("captcha@test")
    for _ in range(3):
        flow.submit_captcha("bad")
    assert pages[-1][0] == "error" and "captcha" in pages[-1][1]["message"].lower()
