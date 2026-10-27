"""Playwright MCP wiring, Muse Image generation, and code charts."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.charts import bar_chart, line_chart
from src.image_gen import generate_image, save_image
from src.playwright_mcp import PlaywrightMcpClient


class FakeMcp:
    def __init__(self):
        self.calls = []
    def list_tools(self):
        return [{"name": "browser_navigate"}]
    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "tool": name, "arguments": arguments}
    def close(self):
        pass


def test_playwright_mcp_wiring_fake():
    fake = FakeMcp()
    client = PlaywrightMcpClient(mcp_client=fake)
    client.start()
    assert client.list_tools()[0]["name"] == "browser_navigate"
    result = client.navigate("https://example.com")
    assert result["tool"] == "browser_navigate"
    assert result["arguments"]["url"] == "https://example.com"
    assert client.snapshot()["tool"] == "browser_snapshot"
    client.close()


def test_playwright_mcp_requires_start():
    client = PlaywrightMcpClient(mcp_client=FakeMcp())
    try:
        client.navigate("https://example.com")
        assert False, "must raise before start"
    except ValueError:
        pass


def test_muse_image_generation_fake():
    fake_bytes = b"\x89PNG fake image"
    def fake_post(url, headers, payload):
        assert "images/generations" in url
        assert payload["model"] == "muse-image-1.0"
        assert headers["Authorization"] == "Bearer test-key"
        return {"data": [{"b64_json": base64.b64encode(fake_bytes).decode()}]}
    data = generate_image("A worm game sprite", "test-key", "https://example.com/v1", post=fake_post)
    assert data == fake_bytes
    try:
        generate_image("Hi", "", "https://example.com/v1", post=fake_post)
        assert False, "must raise without a key, generation is credit-gated"
    except ValueError:
        pass


def test_save_image_and_charts_are_code_not_image_gen(tmp_path):
    path = save_image(b"\x89PNG", tmp_path / "sprite")
    assert path.suffix == ".png"
    assert path.read_bytes() == b"\x89PNG"
    bar = bar_chart("Worm scores", [("Worm", 10), ("Snake", 7)], tmp_path / "bar.svg")
    text = bar.read_text()
    assert "Worm" in text and "10" in text and "<rect" in text
    line = line_chart("Worm growth", [("Day 1", 1), ("Day 2", 3)], tmp_path / "line.svg")
    text = line.read_text()
    assert "Day 1" in text and "3" in text and "<polyline" in text
    try:
        bar_chart("Empty", [], tmp_path / "empty.svg")
        assert False, "empty chart must raise"
    except ValueError:
        pass


def test_muse_image_edit_multiple_refs():
    from src.image_gen import edit_image
    fake_bytes = b"\x89PNG edited"
    def fake_post(url, headers, payload):
        assert "images/edits" in url
        assert payload["model"] == "muse-image-1.0"
        assert len(payload["images"]) == 2
        return {"data": [{"b64_json": base64.b64encode(fake_bytes).decode()}]}
    data = edit_image("Combine @Image1 the product with @Image2 the background, keep @Image1 label unchanged",
                      "test-key", "https://example.com/v1",
                      reference_images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
                      post=fake_post)
    assert data == fake_bytes
    try:
        edit_image("Hi", "test-key", "https://example.com/v1", reference_images=[], post=fake_post)
        assert False, "0 refs must raise"
    except ValueError:
        pass
    try:
        edit_image("Hi", "test-key", "https://example.com/v1", reference_images=["x"] * 11, post=fake_post)
        assert False, "11 refs must raise, max is 10"
    except ValueError:
        pass
