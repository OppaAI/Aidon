"""edit_image tool: local files, approval, and saving under <home>/images."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.capabilities import image_gen
from src.system.config import Config
from src.system.main import _goal_extra_tools


class _Gate:
    def __init__(self):
        self.calls = []

    def ask(self, kind, title, body, **kwargs):
        self.calls.append({"kind": kind, "title": title, "body": body})
        return SimpleNamespace(verdict="approved")


def _setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    uploads = home / "library" / "uploads"
    uploads.mkdir(parents=True)
    photo = uploads / "face.png"
    photo.write_bytes(b"\x89PNGface")
    monkeypatch.setattr(image_gen, "edit_image_files", lambda *a, **k: b"edited-bytes")
    config = Config(home=home)
    config.api_key = "test-key"
    config.api_base = "https://example.com/v1"
    gate = _Gate()
    tools = _goal_extra_tools(config, home / "workspace", gate=gate,
                              optional_tools=True)
    return config, gate, photo, tools


def test_edit_image_saves_under_home_images(tmp_path, monkeypatch):
    config, gate, photo, tools = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style, @Image1", image=str(photo),
                                 output="images/portrait.png")
    saved = config.home / "images" / "portrait.png"
    assert result == f"IMAGE_SAVED: {saved}"
    assert saved.read_bytes() == b"edited-bytes"
    assert len(gate.calls) == 1
    assert gate.calls[0]["kind"] == "live"
    assert "spend image credits" in gate.calls[0]["body"]


def test_edit_image_accepts_upload_basename(tmp_path, monkeypatch):
    _, _, photo, tools = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image=photo.name)
    assert result.startswith("IMAGE_SAVED: ")
    assert Path(result.split(": ", 1)[1]).name.startswith("aidon-")


def test_edit_image_refuses_unknown_reference(tmp_path, monkeypatch):
    _, gate, _, tools = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image="missing.png")
    assert result.startswith("IMAGE_EDIT_FAILED:")
    assert gate.calls == []


def test_edit_image_refuses_output_outside_home(tmp_path, monkeypatch):
    _, _, photo, tools = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image=str(photo),
                                 output="/tmp/elsewhere.png")
    assert result.startswith("IMAGE_EDIT_FAILED:")
