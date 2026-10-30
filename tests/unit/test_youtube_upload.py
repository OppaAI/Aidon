"""YouTube upload: metadata rules, token refresh, chunked
resumable upload with 308 continuation and 5xx retries."""

import json

import pytest

from src.social.youtube_upload import (
    build_metadata, refresh_access_token, truncate, upload_video)


def test_truncate_and_metadata_defaults():
    assert truncate("x" * 101, 100) == "x" * 99 + "…"
    meta = build_metadata("Title", "Desc")
    assert meta["snippet"]["title"] == "Title"
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["snippet"]["categoryId"] == "22"
    assert meta["status"]["selfDeclaredMadeForKids"] is False
    with pytest.raises(ValueError, match="unknown privacy"):
        build_metadata("T", privacy="everywhere")


def _post_ok(url, body, headers, timeout=60.0):
    return 200, json.dumps({"access_token": "tok123"}).encode(), {}


def test_refresh_ok_missing_and_failed():
    assert refresh_access_token("cid", "csec", "rtok",
                                post=_post_ok) == "tok123"
    with pytest.raises(RuntimeError, match="credentials missing"):
        refresh_access_token("", "", "", post=_post_ok)
    def _post_bad(url, body, headers, timeout=60.0):
        return 400, json.dumps({"error": "invalid_grant"}).encode(), {}
    with pytest.raises(RuntimeError, match="token refresh failed"):
        refresh_access_token("cid", "csec", "rtok", post=_post_bad)


def _make_video(tmp_path, data=b"0123456789A"):
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    return path


def _post_init(url, body, headers, timeout=60.0):
    assert "uploadType=resumable" in url
    assert "part=snippet,status" in url
    return 200, b"{}", {"Location": "https://session.invalid/upload"}


def test_chunked_upload_ranges_and_id(tmp_path):
    ranges = []
    calls = {"n": 0}

    def _put(url, body, headers, timeout=120.0):
        assert url == "https://session.invalid/upload"
        ranges.append(headers["Content-Range"])
        calls["n"] += 1
        if calls["n"] < 3:
            return 308, b"", {}
        return 200, json.dumps({"id": "abc123"}).encode(), {}

    result = upload_video(_make_video(tmp_path),
                          build_metadata("T"), "tok123",
                          post=_post_init, put=_put, chunk_size=4)
    assert result["id"] == "abc123"
    assert ranges == ["bytes 0-3/11", "bytes 4-7/11", "bytes 8-10/11"]


def test_upload_retries_5xx_then_succeeds(tmp_path):
    calls = {"n": 0}

    def _put(url, body, headers, timeout=120.0):
        calls["n"] += 1
        if calls["n"] < 3:
            return 500, b"boom", {}
        return 201, json.dumps({"id": "vid9"}).encode(), {}

    result = upload_video(_make_video(tmp_path, b"abcd"),
                          build_metadata("T"), "tok123",
                          post=_post_init, put=_put, chunk_size=64)
    assert result["id"] == "vid9"
    assert calls["n"] == 3


def test_upload_failures(tmp_path):
    def _put_500(url, body, headers, timeout=120.0):
        return 500, b"boom", {}

    with pytest.raises(RuntimeError, match="after retries"):
        upload_video(_make_video(tmp_path, b"abcd"),
                     build_metadata("T"), "tok123",
                     post=_post_init, put=_put_500, chunk_size=64)

    def _post_bad(url, body, headers, timeout=60.0):
        return 400, b"bad request", {}

    with pytest.raises(RuntimeError, match="init failed"):
        upload_video(_make_video(tmp_path, b"abcd"),
                     build_metadata("T"), "tok123",
                     post=_post_bad, put=_put_500, chunk_size=64)

    def _post_no_location(url, body, headers, timeout=60.0):
        return 200, b"{}", {}

    with pytest.raises(RuntimeError, match="no session URL"):
        upload_video(_make_video(tmp_path, b"abcd"),
                     build_metadata("T"), "tok123",
                     post=_post_no_location, put=_put_500, chunk_size=64)

    with pytest.raises(RuntimeError, match="not found"):
        upload_video(tmp_path / "missing.mp4",
                     build_metadata("T"), "tok123",
                     post=_post_init, put=_put_500)

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        upload_video(empty, build_metadata("T"), "tok123",
                     post=_post_init, put=_put_500)


# --- Tool closure: approval card carries the details ---

from types import SimpleNamespace

from src.system.main import _goal_extra_tools


class _Gate:
    def __init__(self, verdict="approved"):
        self.verdict = verdict
        self.seen = []

    def ask(self, kind, title, body, action_class=None, force_ask=False):
        self.seen.append((kind, title, body, force_ask))
        return SimpleNamespace(verdict=self.verdict)


def _yt_tools(tmp_path, gate):
    config = SimpleNamespace(
        home=tmp_path, youtube_client_id="",
        youtube_client_secret="", youtube_refresh_token="",
        youtube_privacy_status="private", youtube_category_id="22")
    return _goal_extra_tools(config, tmp_path, gate=gate)


def test_closure_approval_shows_details_and_missing_creds(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0123")
    gate = _Gate()
    tools = _yt_tools(tmp_path, gate)
    result = tools["youtube_upload"](str(clip), "My Clip", "A desc")
    assert result.startswith("upload failed")
    assert "credentials missing" in result
    kind, title, body, force_ask = gate.seen[-1]
    assert kind == "live" and title == "Publish to YouTube"
    assert "My Clip" in body and "private" in body and force_ask is True


def test_closure_rejection_and_missing_file(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0123")
    gate = _Gate(verdict="rejected")
    tools = _yt_tools(tmp_path, gate)
    assert tools["youtube_upload"](str(clip), "T").startswith(
        "upload rejected")
    gate2 = _Gate()
    tools2 = _yt_tools(tmp_path, gate2)
    assert tools2["youtube_upload"](
        str(tmp_path / "nope.mp4"), "T").startswith("upload refused")
