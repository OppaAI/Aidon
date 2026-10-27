"""YouTube, old Aidon: search and metadata, via the Data API v3.

Old key, only: AIDON_YOUTUBE_API_KEY. No new duplicate.
Search costs quota, 100 units per search, metadata 1 unit,
so search is not free, and a missing key is an error,
never an invented video. youtube_learn combines search
metadata with the timedtext transcript path: one call
returns metadata, transcript, and chapters together.
A get function is injected for tests, production uses
urllib, with a timeout, and the API error, never the
key, is returned on failure.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import quote

from .youtube_transcript import fetch_transcript


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


def youtube_learn(query: str = "", video_id: str = "", api_key: str = "",
                  lang: str = "en", get=None, fetch=None,
                  visual_request: bool = False) -> dict:
    """Combined YouTube learn contract: metadata + transcript + chapters.

    Give a video_id directly, or a query (the first search result is
    used). Returns {"ok", "metadata", "transcript", "chapters"}:
    metadata comes from search (or just the id + watch URL when no
    search was run), transcript from the timedtext captions, chapters
    from the transcript data when the source provides them, else [].
    When no transcript is available, transcript is None and the dict
    says so plainly ("transcript_note") instead of failing or
    inventing text. Visual requests (frames, images, video
    understanding) are refused in the dict: this path is text-only,
    it never pretends to see video.
    """
    if visual_request:
        return {"ok": False,
                "refusal": ("YouTube learn is text-only: search metadata "
                            "and captions only. It cannot view frames, "
                            "images, or video content, and will not "
                            "describe what a video looks like."),
                "metadata": None, "transcript": None, "chapters": []}
    metadata = None
    if video_id:
        metadata = {"video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": "", "channel": ""}
    elif query:
        found = search_videos(query, api_key, get=get)
        if not found.get("ok") or not found.get("results"):
            return {"ok": False,
                    "error": found.get("error") or "no video found",
                    "metadata": None, "transcript": None, "chapters": []}
        metadata = found["results"][0]
        video_id = metadata["video_id"]
    else:
        return {"ok": False, "error": "youtube_learn needs a query or video_id",
                "metadata": None, "transcript": None, "chapters": []}
    if not video_id:
        return {"ok": False, "error": "no video id resolved",
                "metadata": metadata, "transcript": None, "chapters": []}
    raw = fetch_transcript(video_id, lang=lang, fetch=fetch)
    # The timedtext source provides caption text only, no chapter
    # data, so chapters stay [] rather than being guessed.
    if raw:
        return {"ok": True, "metadata": metadata, "transcript": raw,
                "transcript_available": True, "chapters": []}
    return {"ok": True, "metadata": metadata, "transcript": None,
            "transcript_available": False,
            "transcript_note": ("No transcript available for this video: "
                                "it has no captions in the requested language."),
            "chapters": []}
