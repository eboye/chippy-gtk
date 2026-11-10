from chippy import background


def test_autostart_file_outside_flatpak(monkeypatch, tmp_path):
    monkeypatch.setattr(background, "in_flatpak", lambda: False)
    background.set_autostart(True, "reason")
    text = background.autostart_path().read_text()
    assert "Exec=" in text and "--background" in text and "Name=Chippy" in text
    background.set_autostart(False, "reason")
    assert not background.autostart_path().exists()
