"""Automatic redaction helper, for use at record time.

This module is deliberately separate from the existing
redaction in memory extras, and it has a different rule
about email addresses. The existing helper redacts emails
as well as tokens. This helper does NOT redact email
addresses by default, because email addresses are
legitimate recipients in specs and drafts, and redacting
them at record time would break the recipient list a run
needs to act on. What it redacts is credential-like
patterns only: bearer tokens, key-like tokens, and
credential assignments.

Patterns, all conservative, each requiring a credential
shape, never an ordinary word, number, or file path:
- Bearer <token>: "Bearer " followed by a token of 8 or
  more token characters. "Bearer" alone, or a short word
  after it, is not redacted.
- sk-...: "sk-" followed by 16 or more token characters.
  Short "sk-" fragments are left alone.
- aidon_...: "aidon_" followed by 16 or more token
  characters, the stored key format. Short fragments are
  left alone.
- AIDON_<NAME>=<value>: an environment-style assignment,
  key in capitals, value up to whitespace, comma, or
  semicolon. The key is kept, the value is replaced.
- password, token, api_key, api-key, secret, followed by
  ":" or "=", then a value: the key is kept, the value
  is replaced. The bare words "password", "token", or
  "secret" with no ":" or "=" are ordinary text and are
  left alone, as are "token count", "password policy",
  and similar phrases.
- Email addresses, file paths, ordinary numbers, and
  ordinary words are never redacted by this module.

redact_dict applies redact_text to string values
recursively, through dicts, lists, and tuples, leaving
keys and non-string values unchanged.
"""

from __future__ import annotations

import re

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/=]{8,}")
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
_AIDON_KEY_RE = re.compile(r"aidon_[A-Za-z0-9_\-]{16,}")
_ENV_ASSIGN_RE = re.compile(r"(AIDON_[A-Z0-9_]+)=[^\s,;]+")
_CREDENTIAL_ASSIGN_RE = re.compile(
    r"((?:password|token|api[_-]?key|secret))\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    """Replace credential-like patterns with [REDACTED], keep everything else."""
    if not isinstance(text, str):
        return text
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SK_RE.sub("[REDACTED]", text)
    text = _AIDON_KEY_RE.sub("[REDACTED]", text)
    text = _ENV_ASSIGN_RE.sub(r"\1=[REDACTED]", text)
    text = _CREDENTIAL_ASSIGN_RE.sub(r"\1=[REDACTED]", text)
    return text


def redact_dict(data):
    """Redact string values recursively, leave keys and other values alone."""
    if isinstance(data, dict):
        return {key: redact_dict(value) for key, value in data.items()}
    if isinstance(data, list):
        return [redact_dict(value) for value in data]
    if isinstance(data, tuple):
        return tuple(redact_dict(value) for value in data)
    if isinstance(data, str):
        return redact_text(data)
    return data
