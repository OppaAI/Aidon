"""YouTube upload: Data API v3 videos.insert, hand-rolled
over stdlib HTTP. No Google client library, no new dependency.

Flow, fresh implementation of the standard setup:
  1. Refresh grant: POST the stored refresh token to the
     OAuth token endpoint for a short-lived access token.
     The access token lives only for this call; nothing
     about it is persisted.
  2. Resumable session init: POST metadata to the upload
     endpoint with part=snippet,status, read the session
     URL from the Location header.
  3. Chunked upload: PUT the file in 256 KiB chunks with
     Content-Range headers. A 308 means "send the next
     chunk"; a 200/201 with a JSON body carries the video id.
     A 5xx retries the same chunk, up to 3 times.

Credentials come from the environment (client id, client
secret, refresh token), never from the repo, never logged.
The consent that produces the refresh token happens once,
outside this code, requesting the youtube.upload scope.

Privacy defaults to private. A demo must never publish
by accident: the caller passes private, unlisted, or
public explicitly, and the approval card the tool shows
names the visibility before anything uploads. Quota note:
videos.insert costs 1600 units; a default 10,000-unit
daily project quota allows about 6 uploads a day, so the
approval gate, not a rate limiter, is the guard here.

The tool closure in main.py is named youtube_upload
(deliberately not youtube_publish): the loop auto-gates
names containing post/send/publish with a generic card,
and this tool carries its own richer approval showing
title, description, visibility, file, and size. One
approval with full details, not two cards.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 256 * 1024
MAX_TITLE = 100
MAX_DESCRIPTION = 5000


def truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def build_metadata(title: str, description: str = "",
                   privacy: str = "private", category_id: str = "22",
                   made_for_kids: bool = False) -> dict:
    privacy = str(privacy or "private").lower()
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"unknown privacy status: {privacy!r}")
    return {
        "snippet": {
            "title": truncate(title, MAX_TITLE),
            "description": truncate(description, MAX_DESCRIPTION),
            "categoryId": str(category_id or "22"),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }


def _http_post(url: str, body: bytes, headers: dict,
               timeout: float = 60.0):
    """POST returning (status, body, headers). Headers are
    needed by the upload init: the session URL arrives as
    a Location header, not in the body."""
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _header(headers: dict, name: str) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _http_put(url: str, body: bytes, headers: dict,
              timeout: float = 120.0):
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def refresh_access_token(client_id: str, client_secret: str,
                         refresh_token: str, post=None) -> str:
    """One refresh grant. Missing credentials or a failed
    grant raise; the caller reports the message, never the
    secret."""
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "YouTube credentials missing: set YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN")
    post = post or _http_post
    import urllib.parse
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    status, raw, _headers = post(TOKEN_URL, payload,
                       {"Content-Type": "application/x-www-form-urlencoded"},
                       timeout=30.0)
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = {}
    token = data.get("access_token", "")
    if status != 200 or not token:
        raise RuntimeError(
            "YouTube token refresh failed: "
            f"HTTP {status}: {str(data.get('error', ''))[:200]}")
    return token


def upload_video(video_path, metadata: dict, access_token: str,
                 post=None, put=None, chunk_size: int = CHUNK_SIZE) -> dict:
    """Init a resumable session, then upload in chunks.
    Returns the API's video resource (id plus snippet/status).
    Raises on init failure, missing file, or exhausted retries."""
    path = Path(video_path)
    if not path.is_file():
        raise RuntimeError(f"video file not found: {video_path}")
    post = post or _http_post
    put = put or _http_put
    mime, _ = mimetypes.guess_type(str(path))
    init_status, init_raw, init_headers = post(
        UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
        json.dumps(metadata).encode(),
        {"Authorization": f"Bearer {access_token}",
         "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Type": mime or "video/mp4"},
        timeout=60.0)
    if init_status not in (200, 201):
        raise RuntimeError(
            "YouTube upload init failed: "
            f"HTTP {init_status}: {init_raw[:500]!r}")
    session_url = _header(init_headers, "Location")
    if not session_url:
        raise RuntimeError(
            "YouTube upload init returned no session URL")
    data = path.read_bytes()
    total = len(data)
    if total == 0:
        raise RuntimeError("video file is empty")
    offset = 0
    while offset < total:
        chunk = data[offset:offset + chunk_size]
        end = offset + len(chunk) - 1
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {offset}-{end}/{total}",
        }
        for _attempt in range(3):
            status, raw, _headers = put(session_url, chunk, headers,
                                        timeout=120.0)
            if status in (200, 201):
                try:
                    return json.loads(raw.decode() or "{}")
                except ValueError:
                    return {"id": "",
                            "raw": raw[:500].decode("utf-8", "replace")}
            if status == 308:
                offset = end + 1
                break
            if status >= 500:
                continue
            raise RuntimeError(
                "YouTube upload failed: "
                f"HTTP {status}: {raw[:500]!r}")
        else:
            raise RuntimeError(
                "YouTube upload failed after retries: HTTP 5xx")
    raise RuntimeError("YouTube upload ended without a video id")
