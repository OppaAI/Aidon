"""Research cache: fetched pages, kept on disk, searchable offline.

A fetch stores the full page text under a sha256 digest of
the URL, in <home>/research_cache/. Nothing here touches
the network: storing, fetching, and searching are all
local file operations, so a fact already fetched is never
re-fetched to be found again. Each file carries a short
header (URL and fetch time) and the body follows, so a
file is readable on its own and the URL survives even
though the filename is a digest.

Bounded, not just stored: an unbounded cache is a slow
search and a growing disk. Three caps, all env-tunable,
0 meaning no cap: how many files the store keeps
(AIDON_RESEARCH_CACHE_MAX_FILES, default 500, oldest
pruned on store, never the file just written), how many
newest files a search scans (AIDON_RESEARCH_CACHE_MAX_SCAN,
default 200), and how many entries a search returns
(AIDON_RESEARCH_CACHE_MAX_MATCHES, default 100). Per-entry
matching lines stay capped at five.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

_MAX_MATCH_LINES = 5


def _cache_cap(env_name: str, default: int) -> int:
    """Env cap for the cache: int, else default; 0 means no cap."""
    try:
        value = int(os.environ.get(env_name, "") or default)
    except ValueError:
        return default
    return max(0, value)


def digest(url: str, text: str) -> str:
    """sha256 over URL and text, hex. The filename uses the
    URL digest alone, so storing the same URL twice replaces
    the older copy instead of piling up duplicates."""
    return hashlib.sha256(f"{url}\n{text}".encode("utf-8", "replace")).hexdigest()


def _url_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()


def _split_header(content: str) -> tuple[str, str]:
    """Return (url, body) for a cache file's content."""
    lines = content.splitlines()
    url = ""
    body_start = 0
    for index, line in enumerate(lines):
        if line.startswith("URL: "):
            url = line[5:].strip()
        elif line.startswith("Fetched: "):
            continue
        elif line == "":
            body_start = index + 1
            break
    else:
        body_start = len(lines)
    return url, "\n".join(lines[body_start:])


class ResearchCache:
    def __init__(self, home):
        self.home = Path(home)
        self.dir = self.home / "research_cache"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, url: str) -> Path:
        return self.dir / f"{_url_digest(url)}.txt"

    def store(self, url: str, text: str) -> Path:
        """Store a page's full text. Returns the cache file path.

        Prunes oldest files past the keep cap, never the file
        just written: the store is the choke point, so growth
        stops here instead of at search time.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        path = self._path_for(url)
        path.write_text(f"URL: {url}\nFetched: {stamp}\n\n{text}",
                        encoding="utf-8", errors="replace")
        max_files = _cache_cap("AIDON_RESEARCH_CACHE_MAX_FILES", 500)
        if max_files:
            try:
                files = sorted(
                    (p for p in self.dir.glob("*.txt") if p != path),
                    key=lambda p: p.stat().st_mtime)
                for old in files[:max(0, len(files) + 1 - max_files)]:
                    try:
                        old.unlink()
                    except OSError:
                        continue
            except OSError:
                pass
        return path

    def fetch(self, url: str) -> str | None:
        """Cached body for a URL, or None when it was never stored."""
        path = self._path_for(url)
        if not path.is_file():
            return None
        try:
            _, body = _split_header(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None
        return body

    def search_research_cache(self, query: str) -> list[dict]:
        """Cached entries whose body contains the query, newest first.

        Each entry is {"url", "lines"} with up to five matching
        lines, stripped and truncated. An empty query matches
        nothing, and a query with no hits returns an empty list.
        Only the newest files are scanned and only so many
        entries return, so a large cache stays a fast search.
        """
        needle = (query or "").strip().lower()
        if not needle:
            return []
        entries: list[dict] = []
        try:
            files = sorted(self.dir.glob("*.txt"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return []
        max_scan = _cache_cap("AIDON_RESEARCH_CACHE_MAX_SCAN", 200)
        if max_scan:
            files = files[:max_scan]
        max_matches = _cache_cap("AIDON_RESEARCH_CACHE_MAX_MATCHES", 100)
        for path in files:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            url, body = _split_header(content)
            matches = [line.strip()[:200] for line in body.splitlines()
                       if line.strip() and needle in line.lower()]
            if matches:
                entries.append({"url": url, "lines": matches[:_MAX_MATCH_LINES]})
                if max_matches and len(entries) >= max_matches:
                    break
        return entries
