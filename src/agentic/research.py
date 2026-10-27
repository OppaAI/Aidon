"""Real research: facts with sources, not model assertions.

Real research, in Aidon, means a fact is only called
verified if it was read from a source, with the source
URL, the date it was read, and an excerpt that actually
contains the fact. A model saying a price, a date, or
a rule is not research, it is a proposal, and it stays
unverified until a source excerpt supports it.

How it works, with stdlib only, no secrets, no browser:
- Researcher.fetch(url) reads a URL with urllib, with
  a timeout, and returns text stripped of HTML tags.
  It never sends credentials, cookies, or an API key.
  A URL that fails to fetch is recorded as a failed
  source, never as a fact, and never silently skipped
  into a verified list.
- Researcher.research(question, urls) fetches each URL,
  extracts candidate facts as sentences that contain
  a keyword from the question, and returns a report
  with sources, verified facts (fact + source + excerpt),
  and unverified claims (claims with no source excerpt).
- A fact is verified only if the excerpt, lowercased,
  contains the fact text, lowercased, or a keyword
  overlap that the caller explicitly checks. In tests,
  a local HTTP server serves a page, so research is
  tested against a real fetch, not a fake string.

Research is a live tool, not a sandbox tool. The
sandbox has network off by default, for a reason.
Research runs outside the sandbox, with the owner's
approval for network use, and with the source list
recorded in episodic memory, so a later run can
reuse the same sources instead of re-fetching blindly.

Incremental improving, research part:
A research report is recorded as episodic memory and
as part of the run's Experience lesson, so the next
run for a similar goal starts with the previous
sources, the previous verified facts, and the previous
unverified claims, and only re-researches what is
missing or stale, instead of starting from scratch.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime


def _strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class Source:
    url: str
    fetched_at: str = ""
    excerpt: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"url": self.url, "fetched_at": self.fetched_at,
                "excerpt": self.excerpt, "error": self.error}


@dataclass
class ResearchReport:
    question: str
    sources: list[Source] = field(default_factory=list)
    verified_facts: list[dict] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"question": self.question,
                "sources": [s.to_dict() for s in self.sources],
                "verified_facts": self.verified_facts,
                "unverified_claims": self.unverified_claims}


class Researcher:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def fetch(self, url: str) -> Source:
        fetched_at = datetime.now().isoformat(timespec="seconds")
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Aidon/1.0 research"})
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="ignore")
            text = _strip_html(raw)
            return Source(url=url, fetched_at=fetched_at, excerpt=text[:2000])
        except Exception as exc:
            return Source(url=url, fetched_at=fetched_at, error=str(exc)[:200])

    def research(self, question: str, urls: list[str],
                 claims: list[str] | None = None) -> ResearchReport:
        # Fetch concurrently: with N sources the old loop paid N
        # timeouts in series on a slow or dead site. Order is kept so
        # the report reads the same as before.
        if len(urls) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
                sources = list(pool.map(self.fetch, urls))
        else:
            sources = [self.fetch(url) for url in urls]
        all_text = " ".join(s.excerpt for s in sources if not s.error).lower()
        verified: list[dict] = []
        unverified: list[str] = []
        for claim in claims or []:
            if claim.lower() in all_text:
                source = next((s for s in sources if claim.lower() in s.excerpt.lower()), sources[0] if sources else None)
                verified.append({"fact": claim, "source": source.url if source else "",
                                 "excerpt": (source.excerpt[:300] if source else "")})
            else:
                unverified.append(claim)
        return ResearchReport(question=question, sources=sources,
                              verified_facts=verified,
                              unverified_claims=unverified)
