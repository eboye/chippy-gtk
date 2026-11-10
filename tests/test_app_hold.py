import pytest

from chippy.app import ChippyApp


def _app(fake: bool):
    app = ChippyApp()
    app.fake = fake
    calls = []
    app.hold = lambda: calls.append("hold")
    app.release = lambda: calls.append("release")
    return app, calls


def test_real_run_holds_while_background_is_on(monkeypatch):
    monkeypatch.delenv("CHIPPY_FAKE_KEEP_APP_ID", raising=False)
    app, calls = _app(fake=False)
    app.set_background_hold(True)
    app.set_background_hold(True)  # no double hold
    app.set_background_hold(False)
    app.set_background_hold(False)
    assert calls == ["hold", "release"]


@pytest.mark.parametrize(("keep", "expected"), [(None, []), ("1", ["hold", "release"])])
def test_fake_run_holds_only_for_the_nested_check(monkeypatch, keep, expected):
    if keep is None:
        monkeypatch.delenv("CHIPPY_FAKE_KEEP_APP_ID", raising=False)
    else:
        monkeypatch.setenv("CHIPPY_FAKE_KEEP_APP_ID", keep)
    app, calls = _app(fake=True)
    app.set_background_hold(True)
    app.set_background_hold(False)
    assert calls == expected
