"""Web search, Aidon backends, Tavily primary, SearXNG fallback.

Old keys, only: TAVILY_API_KEY, AIDON_SEARCH_BACKEND,
AIDON_SEARXNG_URL. No new duplicate key.
Backend "tavily" needs TAVILY_API_KEY, backend "searxng"
needs AIDON_SEARXNG_URL, default http://localhost:8888.
Search is read-only, no approval needed, results are
title, url, and snippet, never a full page, never a
credential. A missing key or a failed fetch is an
error result, never an invented result.
A post function is injected for tests, production uses
urllib, with a timeout, and the API error, never the
key, is returned on failure.
"""

from __future__ import annotations

import json
import urllib.request


def _post_json(url: str, payload: dict, headers: dict, post=None):
    if post is not None:
        return post(url, payload, headers)
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _get_json(url: str, get=None):
    if get is not None:
        return get(url)
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _filtered(backend: str, results: list, allowlist: list, grants) -> dict:
    if not allowlist:
        return {"ok": True, "backend": backend, "results": results}
    from ..agentic.research_allow import is_fetchable
    kept = [r for r in results if is_fetchable(r.get("url", ""), allowlist, grants)]
    return {"ok": True, "backend": backend, "results": kept,
            "skipped_not_allowlisted": len(results) - len(kept)}


def search(query: str, backend: str = "tavily", tavily_key: str = "",
           searxng_url: str = "http://localhost:8888",
           max_results: int = 5, post=None, get=None, allowlist=None,
           grants=None) -> dict:
    if not query.strip():
        return {"ok": False, "error": "empty query", "results": []}
    # Allowlist wiring: when a list is set, results outside it are
    # dropped and counted as skipped, never returned as verified.
    _allowlist = list(allowlist) if allowlist else []
    try:
        if backend == "tavily":
            if not tavily_key:
                return {"ok": False, "error": "TAVILY_API_KEY not set", "results": []}
            result = _post_json("https://api.tavily.com/search",
                                {"query": query, "max_results": max_results},
                                {"Authorization": f"Bearer {tavily_key}",
                                 "Content-Type": "application/json"}, post=post)
            results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                        "snippet": r.get("content", "")[:300]}
                       for r in result.get("results", [])]
            return _filtered("tavily", results, _allowlist, grants)
        if backend == "searxng":
            from urllib.parse import quote
            result = _get_json(
                f"{searxng_url.rstrip('/')}/search?q={quote(query)}&format=json",
                get=get)
            results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                        "snippet": r.get("content", "")[:300]}
                       for r in result.get("results", [])]
            return _filtered("searxng", results, _allowlist, grants)
        return {"ok": False, "error": f"unknown backend {backend}", "results": []}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "results": []}
