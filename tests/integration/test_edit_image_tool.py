"""edit_image tool: local files, approval, and workspace deliverables."""

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


def _setup(tmp_path, monkeypatch, *, optional_tools=True, image_calls=None):
    home = tmp_path / "home"
    uploads = home / "library" / "uploads"
    uploads.mkdir(parents=True)
    photo = uploads / "face.png"
    photo.write_bytes(b"\x89PNGface")
    def fake_edit(*args, **kwargs):
        if image_calls is not None:
            image_calls.append(kwargs)
        return b"edited-bytes"

    monkeypatch.setattr(image_gen, "edit_image_files", fake_edit)
    config = Config(home=home)
    config.api_key = "test-key"
    config.api_base = "https://example.com/v1"
    gate = _Gate()
    workdir = home / "workspace"
    workdir.mkdir()
    tools = _goal_extra_tools(config, workdir, gate=gate,
                              optional_tools=optional_tools)
    return config, gate, photo, tools, workdir


def test_edit_image_saves_in_workspace_at_requested_path(tmp_path, monkeypatch):
    config, gate, photo, tools, workdir = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style, @Image1", image=str(photo),
                                 output="images/portrait.png")
    saved = workdir / "images" / "portrait.png"
    assert result == f"IMAGE_SAVED: {saved}"
    assert saved.read_bytes() == b"edited-bytes"
    assert len(gate.calls) == 1
    assert gate.calls[0]["kind"] == "live"
    assert "spend image credits" in gate.calls[0]["body"]


def test_edit_image_available_with_optional_tools_disabled_and_still_approved(
        tmp_path, monkeypatch):
    calls = []
    config, gate, photo, tools, workdir = _setup(
        tmp_path, monkeypatch, optional_tools=False, image_calls=calls)
    config.image_model = "muse-image-1.0"
    result = tools["edit_image"]("describe the attached image", image=str(photo))
    assert result.startswith("IMAGE_SAVED: ")
    assert Path(result.split(": ", 1)[1]).is_relative_to(workdir)
    assert gate.calls[0]["kind"] == "live"
    assert calls[0]["model"] == "muse-image-1.0"


def test_edit_image_accepts_upload_basename(tmp_path, monkeypatch):
    _, _, photo, tools, workdir = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image=photo.name)
    assert result.startswith("IMAGE_SAVED: ")
    saved = Path(result.split(": ", 1)[1])
    assert saved.is_relative_to(workdir)
    assert saved.name.startswith("aidon-")


def test_edit_image_refuses_unknown_reference(tmp_path, monkeypatch):
    _, gate, _, tools, _ = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image="missing.png")
    assert result.startswith("IMAGE_EDIT_FAILED:")
    assert gate.calls == []


def test_edit_image_refuses_output_outside_home(tmp_path, monkeypatch):
    _, _, photo, tools, _ = _setup(tmp_path, monkeypatch)
    result = tools["edit_image"]("anime style", image=str(photo),
                                 output="/tmp/elsewhere.png")
    assert result.startswith("IMAGE_EDIT_FAILED:")
