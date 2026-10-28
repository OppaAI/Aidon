"""Natural-language framings for asking, approval, and reports.

Old Aidon's asking, approval, and report messages read like sentences
a person wrote, differently worded each time, because a small local
model wrote them fresh. New Aidon's are templates ("Done: ...",
"Rejected, no live action taken."). This module brings back the
natural, varied wording without spending a model call on it: each
message type has several framings, picked in rotation (approvals,
acks) or by run id (reports, one per run), wrapped around the same
specific content — the action, the files, the error — so the whole
message still differs every time. Titles, card ids, approve/reject
mechanics, and the stats line are untouched; only the human-facing
framing varies.
"""

from __future__ import annotations

import hashlib
import itertools

# Approval cards keep their title and body; only this lead-in varies,
# so the owner still sees exactly what is being asked.
APPROVAL_LEAD_INS = [
    "Before I go ahead, a quick check:",
    "One thing needs your yes before I continue:",
    "I'm ready to take this step — just need your call first:",
    "Quick decision needed before this goes further:",
    "Holding here for your go-ahead:",
    "This one needs a human verdict before I proceed:",
]

# Acks for an answer to a waiting question, on Slack/WhatsApp.
QUESTION_ACKS = [
    "Got it — carrying on with that.",
    "Thanks, that clears it up — continuing.",
    "Noted — picking up where I left off.",
    "Perfect, thanks — I'll go with that.",
    "Understood — continuing with that answer.",
    "Good, that's what I needed — carrying on.",
]

# Report openers, one per run (picked by run id). The files sentence
# and the stats line follow, so the full message always differs.
REPORT_DONE_OPENERS = [
    "Done:",
    "All done:",
    "Finished:",
    "That's done:",
    "Wrapped up:",
    "There we go:",
]

REPORT_FAILED_OPENERS = [
    "That run failed:",
    "It didn't get there:",
    "Something went wrong:",
    "That one didn't make it:",
]

REPORT_REJECTED = [
    "Left as-is — no live action taken.",
    "OK, leaving it alone — nothing went live.",
    "Not going ahead with that — no live action taken.",
    "Understood — I won't take any live action on this.",
]

REPORT_CANCELLED = [
    "Stopped — no further steps ran.",
    "Called off — I stopped where I was.",
    "Cancelled — nothing further ran.",
    "Stopping here — the rest won't run.",
]

_counter = itertools.count()


def _by_key(variants: list[str], key: str) -> str:
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    return variants[int(digest, 16) % len(variants)]


def approval_lead_in() -> str:
    """Next framing in rotation: consecutive approvals read differently."""
    return APPROVAL_LEAD_INS[next(_counter) % len(APPROVAL_LEAD_INS)]


def question_ack() -> str:
    """Next ack in rotation for an answer to a waiting question."""
    return QUESTION_ACKS[next(_counter) % len(QUESTION_ACKS)]


def report_done_opener(run_id: str) -> str:
    return _by_key(REPORT_DONE_OPENERS, run_id or "run")


def report_failed_opener(run_id: str) -> str:
    return _by_key(REPORT_FAILED_OPENERS, run_id or "run")


def report_rejected(run_id: str) -> str:
    return _by_key(REPORT_REJECTED, run_id or "run")


def report_cancelled(run_id: str) -> str:
    return _by_key(REPORT_CANCELLED, run_id or "run")


def files_sentence(files: list[str]) -> str:
    """Natural sentence for what a finished run delivered."""
    names = [str(f) for f in files if str(f).strip()]
    if not names:
        return "Nothing was delivered this time."
    if len(names) == 1:
        return f"{names[0]} is ready."
    return f"{', '.join(names[:-1])} and {names[-1]} are ready."


def report_text(status: str, files: list[str], error: str,
                run_id: str, stats_line: str) -> str:
    """Full completion message for a run, in natural language.

    The stats line (elapsed, tokens) is kept verbatim as the trailing
    line, so the WebUI stats display keeps working; only the human
    sentence above it varies.
    """
    if status == "done":
        head = f"{report_done_opener(run_id)} {files_sentence(files)}"
    elif status == "rejected":
        head = report_rejected(run_id)
    elif status == "cancelled":
        head = report_cancelled(run_id)
    else:
        head = f"{report_failed_opener(run_id)} {error}"
    return f"{head}\n{stats_line}" if stats_line else head
