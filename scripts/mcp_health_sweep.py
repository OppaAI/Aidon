#!/usr/bin/env python3
"""MCP health sweep: probe every known MCP server and record
its health in the capability hub.

Runs where Aidon lives (its home directory holds the
registry, the operator MCP config, and the hub's health
cache). It is an operator script, not a run tool: no
model, no approval card, no plan. It reconnects only to
servers already approved at acquisition or configured
by the operator, so it takes no new grant.

Usage on the Aidon host, after pulling dev:

    python3 scripts/mcp_health_sweep.py --home ~/.aidon

Or daily at 03:00 local time via crontab:

    0 3 * * * python3 ~/Aidon/scripts/mcp_health_sweep.py \
        --home ~/.aidon >> ~/.aidon/health-sweep.log 2>&1

Exit code is 0 whenever the sweep itself ran, even when
servers are down: a down server is a recorded result
(unavailable), not a sweep failure. Exit code 2 means
the sweep crashed before recording anything.

Output is one line per server plus a summary, and stale
health records (servers that no longer exist) are
pruned. No secrets are printed: health notes are the
truncated, server-side error strings the hub already
stores, and tokens are never in the registry config.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=None,
                        help="Aidon home (default: $AIDON_HOME or ~/.aidon)")
    args = parser.parse_args(argv)
    home = Path(args.home or os.environ.get("AIDON_HOME", "~/.aidon")) \
        .expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "src"))
    from src.capabilities.capability_hub import CapabilityHub
    from src.capabilities.mcp_acquisition import check_all_mcp_health
    try:
        results = check_all_mcp_health(home)
        pruned = CapabilityHub(home).prune_health()
    except Exception as exc:
        print(f"SWEEP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for result in results:
        health = result.get("health", "unknown")
        counts[health] = counts.get(health, 0) + 1
        line = f"{result.get('name')}: {health}"
        if result.get("note"):
            line += f" ({result['note']})"
        print(line)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print(f"SWEEP DONE: {len(results)} servers"
          + (f" ({summary})" if summary else "")
          + f"; pruned {pruned} stale health records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
