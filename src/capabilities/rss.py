"""Native RSS/Atom reading with new-item detection.

One-off reads already worked through fetch_url, but the model had to
hand-parse XML and hand-roll the diff. This module is the native
version: parse the feed into items, and remember which item ids a
state file has already seen, so a scheduled goal reports only what
is new. No new scheduler trigger kind: polling is the scheduler's
job (a scheduled goal calling fetch_feed); this module is the
parser plus the dedupe.

State lives in one JSON file (a list of seen ids). The first call
for a state file has no baseline, so every current item is reported
and the baseline is saved; later calls report only new ids. An item
id is its guid, else its link, else its title.
"""

from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

_MAX_ITEMS = 50


def _text(node) -> str:
    if node is None or node.text is None:
        return ""
    return str(node.text).strip()


def _local(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _child(item, name: str):
    for child in item:
        if _local(child.tag) == name:
            return child
    return None


def _link(item) -> str:
    for child in item:
        if _local(child.tag) != "link":
            continue
        href = (child.attrib or {}).get("href", "").strip()
        if href:
            return href
        if _text(child):
            return _text(child)
    return ""


def parse_feed(xml_text: str) -> list[dict]:
    """Parse RSS 2.0 or Atom XML into items.

    Each item is {title, link, id, published}. Unparseable XML
    raises ValueError; callers that must never fail (plan tools)
    catch it and return the failure as text.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable feed XML: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = _text(_child(node, "title"))
        link = _link(node)
        # Elements with no children are falsy, so never use `or`
        # on them: check for None explicitly, or guid/date reads
        # silently fall through to the link/empty fallback.
        guid_node = _child(node, "guid")
        if guid_node is None:
            guid_node = _child(node, "id")
        guid = _text(guid_node)
        pub_node = None
        for name in ("pubdate", "updated", "published", "date"):
            pub_node = _child(node, name)
            if pub_node is not None:
                break
        published = _text(pub_node)
        item_id = guid or link or title
        if not item_id:
            continue
        items.append({"title": title, "link": link, "id": item_id,
                      "published": published})
    return items


def _default_fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Aidon/1.0 (feed reader)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _load_seen(state_path: Path) -> tuple[set, bool]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return set(), True
    if not isinstance(data, list):
        return set(), True
    return {str(x) for x in data}, False


def fetch_feed(url: str, state_path, opener=None) -> str:
    """Fetch a feed and report items not seen in the state file.

    ``opener`` is an injectable fetch (url -> bytes) for tests; the
    default fetches over HTTP with a 30s timeout. Returns a human
    readable summary, never raises: failures come back as text, the
    same contract as fetch_url.
    """
    try:
        raw = (opener(url) if opener is not None
               else _default_fetch(str(url)))
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        items = parse_feed(text)
    except Exception as exc:
        return f"feed failed: {type(exc).__name__}: {exc}"
    path = Path(state_path)
    seen, first_run = _load_seen(path)
    new = [item for item in items if item["id"] not in seen]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted(seen | {item["id"] for item in items})),
            encoding="utf-8")
    except Exception as exc:
        return f"feed failed: could not save state: {exc}"
    if first_run:
        head = f"First check — {len(items)} current item(s) (baseline saved):"
        shown = items[:_MAX_ITEMS]
    elif not new:
        return f"No new items from {url}."
    else:
        head = f"{len(new)} new item(s) from {url}:"
        shown = new[:_MAX_ITEMS]
    lines = [head]
    for item in shown:
        line = f"- {item['title'] or '(untitled)'}"
        if item["link"]:
            line += f" ({item['link']})"
        if item["published"]:
            line += f" [{item['published']}]"
        lines.append(line)
    return "\n".join(lines)
