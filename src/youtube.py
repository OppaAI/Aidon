"""YouTube, old Aidon: search and metadata, via the Data API v3.

Old key, only: AIDON_YOUTUBE_API_KEY. No new duplicate.
Search costs quota, 100 units per search, metadata 1 unit,
so search is not free, and a missing key is an error,
never an invented video. Transcript fetching for a direct
video URL is not built here, it was a separate path in
old Aidon, and is not claimed here.
A get function is injected for tests, production uses
urllib, with a timeout, and the API error, never the
key, is returned on failure.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import quote


def _get_json(url: str, get=None):
    if get is not None:
        return get(url)
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def search_videos(query: str, api_key: str, max_results: int = 5, get=None) -> dict:
    if not api_key:
        return {"ok": False, "error": "AIDON_YOUTUBE_API_KEY not set", "results": []}
    if not query.strip():
        return {"ok": False, "error": "empty query", "results": []}
    try:
        url = ("https://www.googleapis.com/youtube/v3/search"
               f"?part=snippet&type=video&maxResults={max_results}"
               f"&q={quote(query)}&key={api_key}")
        result = _get_json(url, get=get)
        if result.get("error"):
            return {"ok": False, "error": str(result["error"]), "results": []}
        results = []
        for item in result.get("items", []):
            video_id = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            results.append({"video_id": video_id,
                            "title": snippet.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
                            "channel": snippet.get("channelTitle", "")})
        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "results": []}
