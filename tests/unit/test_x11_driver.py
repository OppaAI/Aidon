from src.capabilities.x11_driver import X11Driver
import pytest


def test_x11_driver_uses_fixed_xdotool_commands(monkeypatch):
    calls = []
    monkeypatch.setenv("AIDON_MODEL_API_KEY", "must-not-reach-xdotool")
    monkeypatch.setenv("XAUTHORITY", "/tmp/test-xauthority")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr("src.capabilities.x11_driver.subprocess.run", fake_run)
    driver = X11Driver(executable="xdotool", display=":0")
    driver({"kind": "click", "x": 10, "y": 20})
    driver({"kind": "type", "text": "hello; rm -rf /"})
    driver({"kind": "key", "key": "Return"})
    driver({"kind": "scroll", "delta": -3})

    assert [call[0] for call in calls] == [
        ["xdotool", "mousemove", "--sync", "10", "20"],
        ["xdotool", "click", "1"],
        ["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", "hello; rm -rf /"],
        ["xdotool", "key", "--clearmodifiers", "Return"],
        ["xdotool", "click", "--repeat", "3", "--delay", "10", "5"],
    ]
    assert all(call[1]["check"] is True for call in calls)
    assert all("AIDON_MODEL_API_KEY" not in call[1]["env"] for call in calls)
    assert all(call[1]["env"]["XAUTHORITY"] == "/tmp/test-xauthority"
               for call in calls)


def test_x11_driver_requires_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    try:
        X11Driver(executable="xdotool")
    except RuntimeError as exc:
        assert "DISPLAY" in str(exc)
    else:
        raise AssertionError("driver must refuse without a display")


def test_x11_type_treats_option_like_text_as_literal(monkeypatch):
    calls = []
    monkeypatch.setattr("src.capabilities.x11_driver.subprocess.run",
                        lambda argv, **kwargs: calls.append(argv))
    driver = X11Driver(executable="xdotool", display=":0")
    driver({"kind": "type", "text": "--file /tmp/example"})
    assert calls[0][-2:] == ["--", "--file /tmp/example"]


def test_launch_uses_installed_desktop_id_without_shell_or_secrets(tmp_path, monkeypatch):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "org.gnome.gedit.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Text Editor\nExec=gedit\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path))
    monkeypatch.setenv("AIDON_API_KEY", "not-in-app-environment")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    driver = X11Driver(executable="xdotool", display=":99")
    assert driver.applications() == [{"id": "org.gnome.gedit.desktop", "name": "Text Editor"}]
    driver({"kind": "launch_app", "app_id": "org.gnome.gedit.desktop"})
    assert calls[0][0] == ["/usr/bin/gtk-launch", "org.gnome.gedit.desktop"]
    assert "AIDON_API_KEY" not in calls[0][1]["env"]
    with pytest.raises(ValueError, match="installed"):
        driver({"kind": "launch_app", "app_id": "/tmp/arbitrary.desktop"})
    assert len(calls) == 1
