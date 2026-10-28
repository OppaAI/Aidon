"""One-way social publishing, Aidon: Threads and Instagram.

Old keys, only: AIDON_THREADS_ACCESS_TOKEN,
AIDON_THREADS_ACCOUNT_ID, AIDON_IG_USER_ID,
AIDON_IG_ACCESS_TOKEN. No new duplicate.
Publishing is ALWAYS approval-gated, in code, not by
convention: publish_threads and publish_instagram take
an approval function, and return "not approved" unless
it returns approved. Two-way channels need no approval
for outbound, one-way publishing always does, standing
comms policy, same as Aidon.
Threads: create a container, then publish it, via the
Threads Graph API. Instagram: create a media container,
then publish it, via the Instagram Graph API, photos
only in v1, a public image_url is required by the API.
A missing token or account is an error, never a silent
skip. Post functions are injected for tests, production
uses urllib, and the API error, never the token, is
returned.
"""

from __future__ import annotations

import json
import urllib.request


def _post_json(url: str, payload: dict, post=None):
    if post is not None:
        return post(url, payload)
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def publish_threads(text: str, account_id: str, access_token: str,
                    approve=None, post=None) -> dict:
    if approve is not None and not approve("threads_post", text):
        return {"ok": False, "error": "not approved"}
    if approve is None:
        return {"ok": False, "error": "not approved, no approval gate"}
    if not access_token or not account_id:
        return {"ok": False, "error": "AIDON_THREADS_ACCESS_TOKEN / AIDON_THREADS_ACCOUNT_ID not set"}
    try:
        container = _post_json(
            f"https://graph.threads.net/v1.0/{account_id}/threads",
            {"media_type": "TEXT", "text": text, "access_token": access_token}, post=post)
        container_id = container.get("id", "")
        if not container_id:
            return {"ok": False, "error": str(container.get("error", "no container id"))}
        published = _post_json(
            f"https://graph.threads.net/v1.0/{account_id}/threads_publish",
            {"creation_id": container_id, "access_token": access_token}, post=post)
        return {"ok": True, "id": published.get("id", container_id)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def publish_instagram(image_url: str, caption: str, ig_user_id: str,
                      access_token: str, approve=None, post=None) -> dict:
    if approve is not None and not approve("instagram_post", caption):
        return {"ok": False, "error": "not approved"}
    if approve is None:
        return {"ok": False, "error": "not approved, no approval gate"}
    if not access_token or not ig_user_id:
        return {"ok": False, "error": "AIDON_IG_ACCESS_TOKEN / AIDON_IG_USER_ID not set"}
    try:
        container = _post_json(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media",
            {"image_url": image_url, "caption": caption, "access_token": access_token}, post=post)
        container_id = container.get("id", "")
        if not container_id:
            return {"ok": False, "error": str(container.get("error", "no container id"))}
        published = _post_json(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            {"creation_id": container_id, "access_token": access_token}, post=post)
        return {"ok": True, "id": published.get("id", container_id)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
