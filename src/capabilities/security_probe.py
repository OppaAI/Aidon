"""Security probe, old Aidon security probe concept, rebuilt.

A probe runs the plugin security scan, plus extra checks the scan alone does not do:
network calls, file writes outside the workdir, and secret-like strings in tool code.
It returns findings, it does not approve or adopt anything: adoption is still the
PluginRegistry adopt gate, with the user's approval, and a probe finding is evidence for
that gate, not a decision by itself.
"""

from __future__ import annotations

import re
from pathlib import Path


def probe_plugin(directory) -> list[dict]:
    from .plugins import scan_plugin
    findings = []
    for finding in scan_plugin(Path(directory)):
        findings.append({"type": finding.type, "path": str(finding.path), "line": finding.line, "detail": finding.message})
    directory = Path(directory)
    for path in sorted(directory.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern, label in ((r"requests\b|urllib\.request|socket\.", "network"), (r"open\(.*[\"']/(?!tmp)", "absolute_path_write"), (r"sk-[A-Za-z0-9]{10,}", "secret_like")):
            if re.search(pattern, text):
                findings.append({"type": label, "path": str(path), "line": 0, "detail": f"probe pattern {label} found"})
    return findings
