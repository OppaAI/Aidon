"""Slack file intake and Slack/WhatsApp media sending."""

from src.interface.messaging import (
    MessagingConfig, send_slack_file, send_whatsapp_media)
from src.interface.slack_inbound import parse_slack_event


def test_slack_parse_keeps_non_image_files():
    payload = {"event": {
        "type": "message", "channel": "C1", "user": "U1",
        "text": "<@UBOT> read this",
        "files": [
            {"id": "F1", "name": "plan.pdf", "mimetype": "application/pdf",
             "url_private": "https://files.slack.com/F1"},
            {"id": "F2", "name": "pic.png", "mimetype": "image/png",
             "url_private": "https://files.slack.com/F2"},
        ]}}
    event = parse_slack_event(payload, bot_user_id="UBOT")
    assert event is not None
    assert "[file slack_file_id=F1" in event.goal
    assert "[image slack_file_id=F2" in event.goal
    assert len(event.payload["files"]) == 1
    assert len(event.payload["images"]) == 1
    # A file alone, no text, still becomes a goal.
    alone = parse_slack_event({"event": {
        "type": "message", "channel": "C1", "user": "U1", "text": "<@UBOT>",
        "files": [{"id": "F9", "name": "doc.txt", "mimetype": "text/plain",
                   "url_private": "https://files.slack.com/F9"}]}},
        bot_user_id="UBOT")
    assert alone is not None and "F9" in alone.goal


def _slack_config():
    return MessagingConfig(slack_bot_token="xoxb-test")


def test_send_slack_file_three_steps(tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-fake")
    calls = []
    uploads = []

    def post(url, token, payload):
        calls.append((url, payload))
        if url.endswith("/files.getUploadURLExternal"):
            assert token == "xoxb-test"
            assert payload["filename"] == "report.pdf"
            return {"ok": True, "upload_url": "https://upload.slack.test/x",
                    "file_id": "F123"}
        if url.endswith("/files.completeUploadExternal"):
            assert payload["channel_id"] == "C1"
            assert payload["files"][0]["id"] == "F123"
            assert payload["thread_ts"] == "123.456"
            return {"ok": True}
        raise AssertionError(url)

    def upload(url, token, fields, data, filename):
        uploads.append((url, token, data, filename))

    result = send_slack_file(_slack_config(), "C1", target, post=post,
                             upload=upload, thread_ts="123.456")
    assert result == {"ok": True, "file_id": "F123"}
    assert uploads == [("https://upload.slack.test/x", "", b"%PDF-fake", "report.pdf")]
    assert len(calls) == 2


def test_send_slack_file_failures(tmp_path):
    assert send_slack_file(MessagingConfig(), "C1", tmp_path / "x.pdf")["ok"] is False
    assert send_slack_file(_slack_config(), "C1", tmp_path / "missing.pdf")["ok"] is False


def test_slack_completion_posts_files_as_json_form_field(tmp_path, monkeypatch):
    import json
    from urllib.parse import parse_qs
    from src.interface import messaging

    target = tmp_path / "note.txt"
    target.write_text("hello")
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def urlopen(request, timeout):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(messaging.urllib.request, "urlopen", urlopen)
    result = messaging._post_form_json(
        "https://slack.test/complete", "token",
        {"files": [{"id": "F1", "title": "note.txt"}],
         "channel_id": "C1"})

    assert result == {"ok": True}
    request = captured["request"]
    assert request.get_header("Content-type").startswith(
        "application/x-www-form-urlencoded")
    fields = parse_qs(request.data.decode())
    assert json.loads(fields["files"][0]) == [
        {"id": "F1", "title": "note.txt"}]
    assert fields["channel_id"] == ["C1"]


def test_slack_upload_reservation_uses_form_fields(tmp_path, monkeypatch):
    import json
    from urllib.parse import parse_qs
    from src.interface import messaging

    target = tmp_path / "note.txt"
    target.write_text("hello")
    requests = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    def urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("files.getUploadURLExternal"):
            return Response(b'{"ok":true,"upload_url":"https://upload.slack.test/x",'
                            b'"file_id":"F1"}')
        return Response(b'{"ok":true}')

    monkeypatch.setattr(messaging.urllib.request, "urlopen", urlopen)
    result = messaging.send_slack_file(
        _slack_config(), "C1", target,
        upload=lambda *_args: None)

    assert result == {"ok": True, "file_id": "F1"}
    assert len(requests) == 2
    request = requests[0]
    assert request.get_header("Content-type").startswith(
        "application/x-www-form-urlencoded")
    fields = parse_qs(request.data.decode())
    assert fields["filename"] == ["note.txt"]
    assert fields["length"] == ["5"]
    completion = parse_qs(requests[1].data.decode())
    assert json.loads(completion["files"][0]) == [
        {"id": "F1", "title": "note.txt"}]
    assert completion["channel_id"] == ["C1"]


def test_slack_completion_preserves_api_validation_details(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello")

    def post(url, token, payload):
        if url.endswith("/files.getUploadURLExternal"):
            return {"ok": True, "upload_url": "https://upload.slack.test/x",
                    "file_id": "F1"}
        return {"ok": False, "error": "invalid_arguments",
                "response_metadata": {"messages": [
                    "invalid value for channel_id"]}}

    result = send_slack_file(
        _slack_config(), "C1", target, post=post,
        upload=lambda *_args: None)
    assert result == {
        "ok": False,
        "error": "files.completeUploadExternal: invalid_arguments "
                 "(invalid value for channel_id)",
    }


def _wa_config():
    return MessagingConfig(whatsapp_token="wa-test", whatsapp_phone_id="555")


def test_send_whatsapp_media_image_and_document(tmp_path):
    png = tmp_path / "pic.png"
    png.write_bytes(b"\x89PNG-fake")
    pdf = tmp_path / "menu.pdf"
    pdf.write_bytes(b"%PDF-fake")
    posts = []
    uploads = []

    def post(url, token, payload):
        posts.append(payload)
        return {"messages": [{"id": "wamid.1"}]}

    def upload(url, token, fields, data, filename):
        uploads.append((url, filename))
        assert fields == {"messaging_product": "whatsapp"}
        return {"id": "media-9"}

    result = send_whatsapp_media(_wa_config(), "1555", png, caption="hi",
                                 post=post, upload=upload)
    assert result["ok"] is True and result["media_id"] == "media-9"
    assert posts[-1]["type"] == "image"
    assert posts[-1]["image"] == {"id": "media-9", "caption": "hi"}
    result = send_whatsapp_media(_wa_config(), "1555", pdf,
                                 post=post, upload=upload)
    assert result["ok"] is True
    assert posts[-1]["type"] == "document"
    assert posts[-1]["document"]["filename"] == "menu.pdf"
    assert uploads[1][0].endswith("/555/media")


def test_send_whatsapp_media_failures(tmp_path):
    assert send_whatsapp_media(MessagingConfig(), "1555", tmp_path / "x.png")["ok"] is False
    assert send_whatsapp_media(_wa_config(), "1555", tmp_path / "nope.png")["ok"] is False
