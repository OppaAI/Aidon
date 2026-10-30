"""AIDON_VRM_PATH semantics: blank disables, unset auto-detects.

The drop-in locations stay useful for a fresh install, but clearing the
setting in the WebUI must switch the avatar off instead of quietly
falling back to ~/.aidon/profile/avatar.vrm.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.interface.webui import WebUIServer


def _home_with_drop_in(tmp_path):
    home = tmp_path / "home"
    (home / "profile").mkdir(parents=True)
    vrm = home / "profile" / "avatar.vrm"
    vrm.write_bytes(b"vrm")
    return home, vrm


def test_blank_vrm_path_disables_avatar(tmp_path, monkeypatch):
    home, _ = _home_with_drop_in(tmp_path)
    monkeypatch.setenv("AIDON_VRM_PATH", "")
    assert WebUIServer(home)._avatar_path() is None


def test_unset_vrm_path_keeps_auto_detect(tmp_path, monkeypatch):
    home, vrm = _home_with_drop_in(tmp_path)
    monkeypatch.delenv("AIDON_VRM_PATH", raising=False)
    assert WebUIServer(home)._avatar_path() == vrm


def test_configured_vrm_path_wins(tmp_path, monkeypatch):
    home, _ = _home_with_drop_in(tmp_path)
    chosen = tmp_path / "elsewhere.vrm"
    chosen.write_bytes(b"vrm")
    monkeypatch.setenv("AIDON_VRM_PATH", str(chosen))
    assert WebUIServer(home)._avatar_path() == chosen
