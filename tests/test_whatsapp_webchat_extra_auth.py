"""WhatsApp vision, webchat fast path, extra tools, WebUI auth."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.capabilities.extra_tools import build_extra_tools, dispatch_extra_tool
from src.core.model_client import FakeClient
from src.capabilities.webchat import webchat_reply
from src.interface.webui_auth import is_authorized
from src.capabilities.whatsapp_vision import prepare_vision_message


def test_whatsapp_vision_with_fake_downloader(tmp_path):
    real = tmp_path / "photo.png"
    real.write_bytes(b"\x89PNG fake photo")

    def fake_downloader(media_id):
        assert media_id == "media123"
        return str(real)

    result = prepare_vision_message("media123", "What is in this photo?", downloader=fake_downloader)
    assert result["goal"] == "What is in this photo?"
    assert result["image_path"] == str(real)
    assert result["library_path"] == str(real)
    url = result["image_message"]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_whatsapp_vision_local_path_and_missing_raises(tmp_path):
    real = tmp_path / "photo.jpg"
    real.write_bytes(b"fake jpeg")
    result = prepare_vision_message(str(real), "Describe this")
    assert result["image_path"] == str(real)
    with pytest.raises(ValueError):
        prepare_vision_message(str(tmp_path / "missing.png"), "Describe this")
    with pytest.raises(ValueError):
        prepare_vision_message("no-such-id", "Describe this", downloader=lambda _x: str(tmp_path / "missing.png"))


def test_webchat_reply_uses_researcher_facts():
    client = FakeClient("Vancouver is rainy.")
    facts = ["It rains in Vancouver in October.", "October is autumn."]
    reply = webchat_reply("What is the weather in Vancouver?", lambda goal: facts, client)
    assert reply == "Vancouver is rainy."
    # The prompt sent to the model must contain the facts, verbatim.
    sent = client.calls[0][0].content
    assert "It rains in Vancouver in October." in sent
    assert "October is autumn." in sent


def test_webchat_reply_empty_facts_says_not_found_and_does_not_call_model():
    client = FakeClient("Invented answer, do not use.")
    reply = webchat_reply("What is the weather?", lambda goal: [], client)
    assert "not found" in reply.lower()
    assert client.calls == []


def test_extra_tools_validation_dispatch_and_unknown():
    tools = build_extra_tools({
        "youtube_search": lambda query: f"results for {query}",
        "email_draft": lambda to, subject: f"draft to {to}: {subject}",
    })
    assert dispatch_extra_tool(tools, "youtube_search", {"query": "cats"}) == "results for cats"
    assert dispatch_extra_tool(tools, "email_draft", {"to": "a@example.com", "subject": "Hi"}) == "draft to a@example.com: Hi"
    assert dispatch_extra_tool(tools, "youtube_search", "dogs") == "results for dogs"
    with pytest.raises(ValueError):
        dispatch_extra_tool(tools, "no_such_tool", {})
    with pytest.raises(ValueError):
        build_extra_tools({"bad-name": lambda: "x"})
    with pytest.raises(ValueError):
        build_extra_tools({"1bad": lambda: "x"})
    with pytest.raises(ValueError):
        build_extra_tools({"good": "not callable"})


def test_webui_auth_localhost_remote_and_bearer():
    assert is_authorized({}, None, "127.0.0.1") is True
    assert is_authorized({}, None, "::1") is True
    assert is_authorized({}, None, "localhost") is True
    assert is_authorized({}, None, "203.0.113.10") is False
    assert is_authorized({}, "", "203.0.113.10") is False
    token = "secret-token"
    assert is_authorized({"Authorization": "Bearer secret-token"}, token, "203.0.113.10") is True
    assert is_authorized({"authorization": "Bearer secret-token"}, token, "127.0.0.1") is True
    assert is_authorized({"Authorization": "Bearer wrong"}, token, "203.0.113.10") is False
    assert is_authorized({}, token, "203.0.113.10") is False
    assert is_authorized({}, token, "127.0.0.1") is False
