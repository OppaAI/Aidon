"""Small, fixed-command X11 driver for the computer-use safety wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class X11Driver:
    """Execute only validated computer actions through xdotool.

    This is intentionally not a general shell. Every action becomes a fixed
    xdotool argv list, and the caller remains responsible for approval.
    """

    def __init__(self, executable: str | None = None, display: str = ""):
        self.executable = executable or shutil.which("xdotool")
        self.display = display or os.environ.get("DISPLAY", "")
        if not self.executable:
            raise RuntimeError("xdotool is not installed")
        if not self.display:
            raise RuntimeError("DISPLAY is not set")

    def _environment(self) -> dict:
        # xdotool only needs the X11 connection and a small runtime context.
        # Do not pass model, messaging, or MCP credentials to a child process.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
               if key in os.environ}
        env["DISPLAY"] = self.display
        for key in ("XAUTHORITY",):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def _run(self, args: list[str]):
        return subprocess.run([self.executable, *args], env=self._environment(), check=True,
                              capture_output=True, text=True, timeout=15)

    def applications(self) -> list[dict]:
        """Published desktop IDs only; arbitrary commands are never accepted."""
        import configparser
        roots = [Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))]
        roots += [Path(root) for root in os.environ.get(
            "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if root]
        found = {}
        for root in roots:
            for path in sorted((root / "applications").glob("*.desktop")):
                if path.name in found:
                    continue
                try:
                    parser = configparser.ConfigParser(interpolation=None)
                    parser.read(path, encoding="utf-8")
                    entry = parser["Desktop Entry"]
                    if entry.get("Type") != "Application" or entry.get("Hidden") == "true":
                        found[path.name] = None
                        continue
                    found[path.name] = {"id": path.name, "name": entry.get("Name", path.stem)}
                except (OSError, KeyError, configparser.Error):
                    continue
        return [app for app in found.values() if app]

    def capture_focus(self) -> str:
        window = self._run(["getwindowfocus"]).stdout.strip()
        if not window.isdecimal():
            raise RuntimeError("Cannot identify the focused desktop window")
        return window

    def restore_focus(self, window: str) -> None:
        if not str(window).isdecimal():
            raise ValueError("Invalid desktop window id")
        try:
            self._run(["windowactivate", "--sync", str(window)])
        except subprocess.CalledProcessError:
            # Minimal X11 sessions may have no EWMH window manager.
            self._run(["windowfocus", "--sync", str(window)])

    def __call__(self, action: dict) -> None:
        kind = action["kind"]
        if kind == "launch_app":
            app_id = str(action.get("app_id", ""))
            if app_id not in {app["id"] for app in self.applications()}:
                raise ValueError("Application must be an installed desktop ID")
            launcher = shutil.which("gtk-launch")
            if not launcher:
                raise RuntimeError("gtk-launch is unavailable")
            env = self._environment()
            for key in ("HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR",
                        "DBUS_SESSION_BUS_ADDRESS"):
                if key in os.environ:
                    env[key] = os.environ[key]
            subprocess.run([launcher, app_id], env=env, check=True,
                           capture_output=True, text=True, timeout=15)
        elif kind == "click":
            self._run(["mousemove", "--sync", str(action["x"]), str(action["y"])])
            self._run(["click", "1"])
        elif kind == "type":
            self._run(["type", "--clearmodifiers", "--delay", "10",
                       "--", str(action["text"])])
        elif kind == "key":
            self._run(["key", "--clearmodifiers", str(action["key"])])
        elif kind == "scroll":
            delta = int(action["delta"])
            if delta:
                # X11 wheel buttons: 4 scrolls up, 5 scrolls down.
                self._run(["click", "--repeat", str(min(abs(delta), 100)),
                           "--delay", "10", "4" if delta > 0 else "5"])
        else:
            raise ValueError(f"X11 driver cannot execute {kind}")


def x11_available() -> bool:
    return bool(shutil.which("xdotool") and os.environ.get("DISPLAY"))
