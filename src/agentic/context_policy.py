"""Small, deterministic rules for deciding when chat history applies."""

from __future__ import annotations

import re


_PRIOR_REFERENCE = re.compile(
    r"\b(?:previous|earlier|before|yesterday|last time|we discussed|"
    r"as discussed|remember|same (?:image|photo|picture|offer|one))\b",
    re.IGNORECASE,
)


def explicitly_refers_to_prior_context(text: str) -> bool:
    """Whether the current request clearly asks to use earlier conversation."""
    return bool(_PRIOR_REFERENCE.search(str(text or "")))
