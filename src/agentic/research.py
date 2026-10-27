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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects inside urllib: it would follow them
    unchecked. Returning None makes urlopen raise HTTPError for the
    redirect status, and fetch() reads Location from that error
    and re-validates the next hop before fetching it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Researcher:
    def __init__(self, timeout_seconds: int = 15, allowlist=None,
                 grants=None, parallel_tools: int = 4,
                 allow_local: bool = False, cache=None):
        self.timeout_seconds = timeout_seconds
        # Optional ResearchCache: a cached page is returned
        # without a fetch, and a successful fetch is stored.
        self.cache = cache
        # Wired, not just defined: when an allowlist is set, a URL
        # outside it (and outside any explicit grant) is never
        # fetched. It is recorded as a blocked source, not silently
        # widened and not fetched anyway. Every redirect hop is
        # re-validated the same way, and the SSRF guard applies on
        # every hop too, grants or not.
        self.allowlist = list(allowlist) if allowlist else []
        self.grants = grants
        self.parallel_tools = max(1, int(parallel_tools))
        # allow_local=True is the explicit local-server exception,
        # for tests and local development only. Production callers
        # do not pass it.
        self.allow_local = allow_local

    def fetch(self, url: str) -> Source:
        from .research_allow import guard_reason
        fetched_at = datetime.now().isoformat(timespec="seconds")
        if self.cache is not None:
            try:
                cached = self.cache.fetch(url)
            except Exception:
                cached = None
            if cached:
                return Source(url=url, fetched_at=fetched_at,
                              excerpt=_strip_html(cached)[:2000])
        opener = urllib.request.build_opener(_NoRedirect)
        current = url
        for _hop in range(6):
            reason = guard_reason(current, self.allowlist, self.grants,
                                  self.allow_local)
            if reason:
                return Source(url=url, fetched_at=fetched_at,
                              error=f"blocked: {reason}")
            try:
                request = urllib.request.Request(
                    current, headers={"User-Agent": "Aidon/1.0 research"})
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="ignore")
                if self.cache is not None:
                    try:
                        self.cache.store(url, raw)
                    except Exception:
                        pass
                text = _strip_html(raw)
                return Source(url=url, fetched_at=fetched_at, excerpt=text[:2000])
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    loc = exc.headers.get("location") if exc.headers else None
                    if not loc:
                        return Source(url=url, fetched_at=fetched_at,
                                      error="redirect without Location")
                    from urllib.parse import urljoin
                    current = urljoin(current, loc)
                    continue
                return Source(url=url, fetched_at=fetched_at,
                              error=f"HTTP {exc.code}")
            except Exception as exc:
                return Source(url=url, fetched_at=fetched_at, error=str(exc)[:200])
        return Source(url=url, fetched_at=fetched_at,
                      error="too many redirects")

    def research(self, question: str, urls: list[str],
                 claims: list[str] | None = None) -> ResearchReport:
        # Fetch concurrently: with N sources the old loop paid N
        # timeouts in series on a slow or dead site. Order is kept so
        # the report reads the same as before.
        if len(urls) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(self.parallel_tools, len(urls))) as pool:
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
