"""Core file tools must stay inside their sandbox work directory."""

import pytest

from src.capabilities.tools import ToolRegistry


def test_read_and_write_refuse_traversal(tmp_path):
    workdir = tmp_path / "work"
    outside = tmp_path / "private.txt"
    outside.write_text("keep private", encoding="utf-8")
    tools = ToolRegistry(workdir)

    with pytest.raises(ValueError, match="outside workdir"):
        tools.call("read", {"file": "../private.txt"})
    with pytest.raises(ValueError, match="outside workdir"):
        tools.call("write", {"file": "../private.txt", "text": "overwritten"})

    assert outside.read_text(encoding="utf-8") == "keep private"


def test_read_and_write_refuse_symlink_escape(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("keep private", encoding="utf-8")
    (workdir / "private-link.txt").symlink_to(outside)
    tools = ToolRegistry(workdir)

    with pytest.raises(ValueError, match="outside workdir"):
        tools.call("read", {"file": "private-link.txt"})
    with pytest.raises(ValueError, match="outside workdir"):
        tools.call("write", {"file": "private-link.txt", "text": "overwritten"})

    assert outside.read_text(encoding="utf-8") == "keep private"
