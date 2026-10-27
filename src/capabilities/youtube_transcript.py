"""YouTube transcript path, stdlib only, no new dependency.

YouTube captions are fetched from the timedtext endpoint for
a video id, in English by default, and parsed from XML into
plain text. A video with no captions returns an empty string,
never invented text. The fetcher is injectable, so tests use
a fake timedtext XML response and no network call, and a
live run uses urllib with a 15 second timeout.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET


def fetch_transcript(video_id: str, lang: str = "en", fetch=None) -> str:
    url = f"https://video.google.com/timedtext?lang={lang}&v={video_id}"
    if fetch is not None:
        raw = fetch(url)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "Aidon/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="ignore")
    if not raw.strip():
        return ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    parts = []
    for element in root.iter("text"):
        if element.text:
            parts.append(element.text.strip())
    return " ".join(parts)
