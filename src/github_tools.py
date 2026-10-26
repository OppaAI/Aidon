"""GitHub, old Aidon: search and fetch, with an optional token.

Old key, only: GITHUB_TOKEN. No new duplicate.
A token is optional, it only raises the rate limit,
unauthenticated is 10 requests per minute in old Aidon,
and a rate limit is an error, never a retry loop.
Search is repositories, fetch is a file at a ref,
with the raw media type, never the wrong +raw type,
which returns a JSON envelope, not the file.
A get function is injected for tests, production uses
urllib, and the API error, never the token, is returned.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from urllib.parse import quote


def _get(url: str, token: str = "", get=None, headers_extra: dict | None = None):
    if get is not None:
        return get(url, token)
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "aidon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers_extra:
        headers.update(headers_extra)
    request = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers


def search_repos(query: str, token: str = "", get=None) -> dict:
    if not query.strip():
        return {"ok": False, "error": "empty query", "results": []}
    try:
        body, _ = _get(f"https://api.github.com/search/repositories?q={quote(query)}",
                       token, get=get)
        result = json.loads(body) if isinstance(body, (bytes, str)) else body
        if isinstance(result, dict) and result.get("message"):
            return {"ok": False, "error": str(result["message"]), "results": []}
        results = [{"full_name": r.get("full_name", ""), "url": r.get("html_url", ""),
                    "description": r.get("description", "")}
                   for r in result.get("items", [])]
        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "results": []}


def fetch_file(owner: str, repo: str, path: str, ref: str = "main",
               token: str = "", get=None) -> dict:
    try:
        body, _ = _get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
                       token, get=get,
                       headers_extra={"Accept": "application/vnd.github.raw"})
        if isinstance(body, dict):
            content = body.get("content", "")
            text = base64.b64decode(content).decode() if content else ""
            return {"ok": True, "text": text}
        text = body.decode() if isinstance(body, bytes) else str(body)
        return {"ok": True, "text": text}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "text": ""}
