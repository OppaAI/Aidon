"""Deep research, built fresh from the idea, not imported code.

Aiko-chan's research toolkit (adaptive search, deep research) is
pre-hackathon code, so Aidon does not import it: the hackathon
requires new work, and this module rebuilds the IDEA from
scratch, in Aidon's own shape:

- adaptive search: snippets first, fetch only what a judge
  says to ESCALATE — one judge call per round over all
  snippets, never one call per snippet;
- multi-round: when the evidence is thin, follow-up queries
  fill the gaps, bounded by max_rounds;
- condense: fetched pages are scored chunk by chunk against
  the query, and only the best chunks survive, each carrying
  its source URL — so the final answer cites, not just claims.

No new dependency: search and fetch are injected callables.
The plan tool passes Aidon's own search (Tavily/SearXNG) and
read_document (stdlib urllib fetch + MarkItDown/parsers).
Without a model client the judge is heuristic (top sources)
and the summary is extractive; with one, the judge escalates
and the summary is synthesized with inline citations.
"""

from __future__ import annotations

import json
import re

from ..system.model_client import Message

_FETCH_FAILURE_PREFIXES = ("fetch failed", "READ_BLOCKED", "READ_FAILED")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if len(w) > 2}


def score_chunk(chunk: str, query_words: set[str]) -> int:
    """Query-word overlap: how much this chunk is about the query."""
    if not query_words:
        return 0
    return len(_words(chunk) & query_words)


def condense(text: str, query: str, max_chars: int = 2000) -> str:
    """Keep the best chunks of a fetched page, in original order.

    Pages are split on blank lines; chunks are scored by query
    overlap and taken best-first until max_chars, then restored
    to document order so the condensed text still reads.
    """
    chunks = [c.strip() for c in re.split(r"\n\s*\n", str(text)) if c.strip()]
    if not chunks:
        return ""
    query_words = _words(query)
    scored = sorted(enumerate(chunks),
                    key=lambda pair: (-score_chunk(pair[1], query_words),
                                      pair[0]))
    kept: list[tuple[int, str]] = []
    total = 0
    for index, chunk in scored:
        if kept and total + len(chunk) + 2 > max_chars:
            continue
        kept.append((index, chunk))
        total += len(chunk) + 2
        if total >= max_chars:
            break
    kept.sort()
    return "\n\n".join(chunk for _, chunk in kept)


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def _normalize_results(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("results", []) or []
    items = []
    for entry in raw or []:
        if isinstance(entry, dict):
            items.append({"title": str(entry.get("title", "")),
                          "url": str(entry.get("url", "")),
                          "snippet": str(entry.get("snippet", ""))})
        else:
            items.append({"title": "", "url": "", "snippet": str(entry)})
    return items


def _judge_fetch_indices(results: list[dict], query: str, chat,
                         max_sources: int) -> list[int]:
    """Which snippets deserve a full fetch. One call, all snippets."""
    if chat is None:
        return list(range(min(max_sources, len(results))))
    listing = "\n".join(f"{i}. {r['title']}: {r['snippet'][:300]}"
                         for i, r in enumerate(results))
    try:
        reply = chat([Message(role="system", content=(
            "Which of these search snippets need a full page fetch "
            "to answer the query? Return JSON only, exactly "
            "{\"fetch\": [indices]}. Snippets that already answer "
            "the query need no fetch.")),
            Message(role="user", content=f"Query: {query}\n{listing}")])
    except Exception:
        return list(range(min(max_sources, len(results))))
    data = _extract_json(str(reply))
    if not isinstance(data, dict) or not isinstance(data.get("fetch"), list):
        return list(range(min(max_sources, len(results))))
    indices = [int(i) for i in data["fetch"]
               if isinstance(i, int) and 0 <= i < len(results)]
    return indices[:max_sources]


def _follow_up_queries(query: str, evidence: list[dict], chat) -> list[str]:
    if chat is None:
        return []
    listing = "\n".join(f"- {e['url']}: {e['text'][:200]}"
                         for e in evidence)
    try:
        reply = chat([Message(role="system", content=(
            "Suggest up to 2 follow-up search queries that would "
            "fill gaps in this evidence. Return JSON only, exactly "
            "{\"queries\": [\"...\", ...]}. Empty list when the "
            "evidence already answers the query.")),
            Message(role="user", content=f"Query: {query}\n{listing}")])
    except Exception:
        return []
    data = _extract_json(str(reply))
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
        return []
    return [str(q).strip() for q in data["queries"]
            if str(q).strip()][:2]


def deep_research(query: str, search_fn, fetch_fn, chat=None,
                  max_rounds: int = 2, max_sources: int = 5) -> dict:
    """Search, fetch, condense, and synthesize with citations.

    search_fn(query) returns result dicts (title/url/snippet).
    fetch_fn(url) returns page text. chat, when given, is a
    callable taking Message lists and returning reply text.
    Returns query, evidence [{url, title, text}], summary,
    citations [urls in evidence order], and rounds run.
    """
    query = str(query)
    evidence: list[dict] = []
    seen_urls: set[str] = set()
    queries = [query]
    rounds = 0
    for round_index in range(max(1, max_rounds)):
        rounds = round_index + 1
        round_results: list[dict] = []
        for query_text in queries:
            try:
                round_results.extend(_normalize_results(search_fn(query_text)))
            except Exception:
                continue
        if not round_results:
            break
        for index in _judge_fetch_indices(round_results, query, chat,
                                          max_sources):
            result = round_results[index]
            url = result["url"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                text = fetch_fn(url)
            except Exception:
                continue
            if not text or str(text).startswith(_FETCH_FAILURE_PREFIXES):
                continue
            condensed = condense(str(text), query)
            if condensed:
                evidence.append({"url": url, "title": result["title"],
                                 "text": condensed})
        if len(evidence) >= 2 or round_index + 1 >= max(1, max_rounds):
            break
        queries = _follow_up_queries(query, evidence, chat)
        if not queries:
            break
    citations = [e["url"] for e in evidence]
    if chat is not None and evidence:
        listing = "\n\n".join(f"Source {e['url']}:\n{e['text']}"
                               for e in evidence)
        try:
            summary = chat([Message(role="system", content=(
                "Summarize for the query using ONLY the evidence "
                "below. Cite each claim with its source URL inline, "
                "like [url]. Plain text, no invented facts.")),
                Message(role="user", content=f"Query: {query}\n{listing}")])
            summary = str(summary).strip()
        except Exception:
            summary = ""
        if not summary:
            summary = "\n\n".join(e["text"] for e in evidence)
    else:
        summary = "\n\n".join(e["text"] for e in evidence)
    if citations:
        summary += "\n\nSources:\n" + "\n".join(f"- {url}" for url in citations)
    return {"query": query, "evidence": evidence, "summary": summary,
            "citations": citations, "rounds": rounds}
