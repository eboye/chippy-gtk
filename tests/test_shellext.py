import json

import pytest

from chippy.shellext import MARKER, UUID, Installer, ShellExtError, live_shell_allowed


def test_live_shell_allowed():
    assert live_shell_allowed(False, {})  # the real app
    assert live_shell_allowed(False, {"CHIPPY_FAKE_KEEP_APP_ID": "0"})
    assert not live_shell_allowed(True, {})  # a fake on the live session: never
    assert not live_shell_allowed(True, {"CHIPPY_FAKE_KEEP_APP_ID": "0"})
    assert live_shell_allowed(True, {"CHIPPY_FAKE_KEEP_APP_ID": "1"})  # the nested check


class FakeShell:
    def __init__(self, known=False):
        self._known, self.calls = known, []

    def known(self, uuid):
        return self._known

    def enable(self, uuid):
        self.calls.append(("enable", uuid))
        return True

    def disable(self, uuid):
        self.calls.append(("disable", uuid))
        return True


def _source(tmp_path, version=1):
    src = tmp_path / "src" / UUID
    src.mkdir(parents=True, exist_ok=True)
    (src / "metadata.json").write_text(json.dumps({"uuid": UUID, "version": version}))
    (src / "extension.js").write_text("// ext")
    return src


def test_install_copies_marks_and_enables(tmp_path):
    shell = FakeShell(known=False)
    inst = Installer(_source(tmp_path), tmp_path / "exts", shell)
    assert inst.install() is True  # unknown to Shell yet → re-login needed
    assert (inst.target / "extension.js").read_text() == "// ext"
    assert (inst.target / MARKER).exists() and not inst.target.is_symlink()
    assert shell.calls == [("enable", UUID)] and inst.installed()


def test_update_only_when_newer(tmp_path):
    shell = FakeShell(known=True)
    inst = Installer(_source(tmp_path, 1), tmp_path / "exts", shell)
    assert inst.install() is False
    assert inst.update_if_needed() is False
    _source(tmp_path, 2)
    assert inst.update_if_needed() is True
    assert json.loads((inst.target / "metadata.json").read_text())["version"] == 2


def test_remove(tmp_path):
    shell = FakeShell(known=True)
    inst = Installer(_source(tmp_path), tmp_path / "exts", shell)
    inst.install()
    inst.remove()
    assert not inst.target.exists()
    assert shell.calls[-1] == ("disable", UUID)
    inst.remove()  # removing twice is fine


def test_refuses_a_symlink_and_leaves_its_target_alone(tmp_path):
    precious = tmp_path / "repo-extension"
    precious.mkdir()
    (precious / "keep.js").write_text("x")
    root = tmp_path / "exts"
    root.mkdir()
    (root / UUID).symlink_to(precious)
    inst = Installer(_source(tmp_path), root, FakeShell())
    with pytest.raises(ShellExtError, match="symlink"):
        inst.install()
    with pytest.raises(ShellExtError, match="symlink"):
        inst.remove()
    assert (precious / "keep.js").exists()


def test_refuses_a_folder_it_did_not_install(tmp_path):
    root = tmp_path / "exts"
    (root / UUID).mkdir(parents=True)
    (root / UUID / "mine.js").write_text("x")
    shell = FakeShell()
    inst = Installer(_source(tmp_path), root, shell)
    with pytest.raises(ShellExtError, match="wasn't installed by Chippy"):
        inst.remove()
    assert shell.calls == []  # the Shell's state for that extension is left alone too
    assert (root / UUID / "mine.js").exists()
