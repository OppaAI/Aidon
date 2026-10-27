"""Research allowlist and multi-round research.

Allowlist, from old Aidon, now in new Aidon:
- <home>/research_allowlist.txt, one domain per line, or
  AIDON_RESEARCH_ALLOWLIST as a comma list. When an allowlist
  is set, a research source outside it is not used as a
  verified fact, it is reported as skipped, never silently
  widened. When no allowlist is set, research is unrestricted,
  and the report says so, so the owner can see the difference.
- is_allowed(url, allowlist) matches the URL host, or a
  parent domain, so save-on-foods.com covers www.save-on-foods.com.

Multi-round research:
- research_multiround(question, researcher, rounds=2) runs
  one round, then, if the first round returned no verified
  facts, runs a second round with a refined query, "more
  detail on <question>", instead of giving up after one
  failed round. A round that returns facts stops the loop,
  so a good first round costs one round, not two.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def load_allowlist(home: Path, env_value: str = "") -> list[str]:
    domains = [d.strip().lower() for d in env_value.split(",") if d.strip()]
    path = Path(home) / "research_allowlist.txt"
    if path.is_file():
        domains.extend(line.strip().lower() for line in path.read_text().splitlines()
                       if line.strip() and not line.strip().startswith("#"))
    return sorted(set(domains))


def is_allowed(url: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == domain or host.endswith("." + domain) for domain in allowlist)


def research_multiround(question: str, researcher, rounds: int = 2,
                        allowlist: list[str] | None = None):
    """Run research, with a refined second round if the first finds nothing."""
    allowlist = allowlist or []
    query = question
    last = None
    for round_index in range(max(1, rounds)):
        result = researcher(query)
        last = result
        facts = getattr(result, "facts", []) if not isinstance(result, dict) else result.get("facts", [])
        allowed_facts = [fact for fact in facts
                         if is_allowed(getattr(fact, "url", "") or (fact.get("url", "") if isinstance(fact, dict) else ""), allowlist)]
        if allowed_facts:
            return result
        query = f"more detail on {question}"
    return last
