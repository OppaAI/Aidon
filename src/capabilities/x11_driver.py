"""Small, fixed-command X11 driver for the computer-use safety wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess


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

    def _run(self, args: list[str]) -> None:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        subprocess.run([self.executable, *args], env=env, check=True,
                       capture_output=True, text=True, timeout=15)

    def __call__(self, action: dict) -> None:
        kind = action["kind"]
        if kind == "click":
            self._run(["mousemove", "--sync", str(action["x"]), str(action["y"])])
            self._run(["click", "1"])
        elif kind == "type":
            self._run(["type", "--clearmodifiers", "--delay", "10",
                       str(action["text"])])
        elif kind == "key":
            self._run(["key", "--clearmodifiers", str(action["key"])])
        elif kind == "scroll":
            self._run(["scroll", str(action["delta"])])
        else:
            raise ValueError(f"X11 driver cannot execute {kind}")


def x11_available() -> bool:
    return bool(shutil.which("xdotool") and os.environ.get("DISPLAY"))
