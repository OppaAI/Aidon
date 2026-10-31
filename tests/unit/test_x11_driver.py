from src.capabilities.x11_driver import X11Driver


def test_x11_driver_uses_fixed_xdotool_commands(monkeypatch):
    calls = []

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
        ["xdotool", "type", "--clearmodifiers", "--delay", "10", "hello; rm -rf /"],
        ["xdotool", "key", "--clearmodifiers", "Return"],
        ["xdotool", "click", "--repeat", "3", "--delay", "10", "5"],
    ]
    assert all(call[1]["check"] is True for call in calls)


def test_x11_driver_requires_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    try:
        X11Driver(executable="xdotool")
    except RuntimeError as exc:
        assert "DISPLAY" in str(exc)
    else:
        raise AssertionError("driver must refuse without a display")
