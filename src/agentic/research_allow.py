"""Research allowlist and multi-round research.

Research allowlist used by Aidon:
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

import json
from pathlib import Path
from urllib.parse import urlparse


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


class DomainGrants:
    """Domain grants on top of the configured allowlist, Aidon shape.

    The configured allowlist stays untouched. A grant is either
    run-scoped (ends when the run finishes, never saved) or permanent
    (saved in <home>/domain_grants.json, loaded by every future run).
    A grant is granted by an explicit owner decision, never by
    auto-approve and never by graduated autonomy — the grant flow is
    outside the autonomy policy on purpose, so trust in an action
    class can never widen network reach.
    """

    def __init__(self, home: Path | None = None):
        self._run_grants: set[str] = set()
        self._perm_grants: set[str] = set()
        self._file = Path(home) / "domain_grants.json" if home is not None else None
        if self._file is not None and self._file.is_file():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._perm_grants = {str(d).lower() for d in raw}
            except Exception:
                pass

    def grant_run(self, host: str) -> None:
        self._run_grants.add(host.lower())

    def grant_permanent(self, host: str) -> None:
        host = host.lower()
        self._perm_grants.add(host)
        if self._file is not None:
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._file.write_text(json.dumps(sorted(self._perm_grants), indent=1) + "\n",
                                      encoding="utf-8")
            except OSError:
                pass  # the run grant below still holds for this run

    def revoke(self, host: str) -> None:
        host = host.lower()
        self._run_grants.discard(host)
        self._perm_grants.discard(host)
        if self._file is not None:
            try:
                self._file.write_text(json.dumps(sorted(self._perm_grants), indent=1) + "\n",
                                      encoding="utf-8")
            except OSError:
                pass

    def is_granted(self, url: str) -> bool:
        host = host_of(url)
        for domain in list(self._perm_grants) + list(self._run_grants):
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def granted_hosts(self) -> list[str]:
        return sorted(self._perm_grants | self._run_grants)


def is_fetchable(url: str, allowlist: list[str] | None = None,
                 grants: DomainGrants | None = None) -> bool:
    """True when a URL may be fetched: allowlisted, granted, or unrestricted."""
    if not allowlist:
        return True
    return is_allowed(url, allowlist) or bool(grants and grants.is_granted(url))


def ssrf_blocked(url: str) -> str | None:
    """SSRF guard: a reason string when the URL's host resolves to a
    non-public address, else None.

    The allowlist matches host STRINGS; an allowlisted host can still
    302 (or DNS-resolve) to 127.0.0.1 / the LAN / cloud metadata, so
    the check runs on the resolved addresses, on the original URL and
    on every redirect hop. IP-literal hosts skip DNS. A host that
    cannot be resolved here is left to the fetch itself (which will
    fail the same way); a host that resolves to ANY non-public
    address blocks. Grants never bypass this guard: a grant widens
    the allowlist, it never widens the network.
    """
    import ipaddress
    import socket
    host = urlparse(url).hostname
    if not host:
        return "URL has no host"
    try:
        literal = ipaddress.ip_address(host)
        addrs = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):
            return None
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    for addr in addrs:
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast
                or addr.is_unspecified):
            return f"host {host} resolves to non-public address {addr}"
    return None


def guard_reason(url: str, allowlist: list[str] | None = None,
                 grants: DomainGrants | None = None,
                 allow_local: bool = False) -> str | None:
    """One check for one hop: SSRF first, then allowlist. None = may fetch.

    allow_local=True is the explicit local-server exception, for
    tests and local development only. Production callers do not
    pass it, so a local address is blocked even if it is
    allowlisted — the allowlist is a host rule, not an SSRF waiver.
    """
    if not allow_local:
        blocked = ssrf_blocked(url)
        if blocked:
            return f"{blocked} (SSRF guard)"
    if allowlist and not is_fetchable(url, allowlist, grants):
        return "host not on research allowlist"
    return None


class StuckDetector:
    """Stuck-iteration watchdog: the same result N times in a row stops a loop.

    AIDON_MAX_STUCK_ITERATIONS, default 8. A research loop
    that keeps returning the identical signature is not making
    progress, it is spinning; the caller breaks, reports the stuck
    signature, and does not burn the rest of the budget on repeats.
    """

    def __init__(self, max_stuck_iterations: int = 8):
        self.max_stuck = max(1, int(max_stuck_iterations))
        self._last: object = None
        self._count = 0

    def see(self, signature) -> bool:
        """Record one iteration. Returns True when the loop is stuck."""
        if signature == self._last:
            self._count += 1
        else:
            self._last = signature
            self._count = 1
        return self._count >= self.max_stuck

    @property
    def count(self) -> int:
        return self._count


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
                        allowlist: list[str] | None = None,
                        max_stuck_iterations: int = 8):
    """Run research, with a refined second round if the first finds nothing.

    rounds is capped by the caller from AIDON_RESEARCH_MAX_ROUNDS
    (old default 10). A stuck loop — the same result signature
    max_stuck_iterations times in a row — breaks early instead of
    burning rounds on repeats.
    """
    allowlist = allowlist or []
    query = question
    last = None
    stuck = StuckDetector(max_stuck_iterations)
    for round_index in range(max(1, rounds)):
        result = researcher(query)
        last = result
        facts = getattr(result, "facts", []) if not isinstance(result, dict) else result.get("facts", [])
        allowed_facts = [fact for fact in facts
                         if is_allowed(getattr(fact, "url", "") or (fact.get("url", "") if isinstance(fact, dict) else ""), allowlist)]
        if allowed_facts:
            return result
        # Stuck signature: the facts (or the whole result) unchanged.
        signature = tuple(sorted(str(f) for f in facts)) if facts else str(result)
        if stuck.see(signature):
            break
        query = f"more detail on {question}"
    return last
