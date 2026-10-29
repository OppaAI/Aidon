"""Natural-language framings for asking, approval, and reports.

Asking, approval, and report messages read like sentences
a person wrote, differently worded each time. The run's own
messages are templates ("Done: ...",
"Rejected, no live action taken."). This module keeps the
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


# Prompts for the model-written framings. The model writes ONLY the
# framing: facts (action, files, error, ids, instructions) are always
# inserted by code, never by the model, so a fanciful reply cannot
# garble what is being asked or delivered.
PHRASE_PROMPTS = {
    "approval": ("Write one short sentence asking the owner to approve "
                 "the next step, under 15 words. Do not describe the "
                 "step itself, just ask for the decision."),
    "report-done": ("Write a short opening phrase for a task-completion "
                    "message, under 10 words, like 'All done:' or "
                    "'Wrapped up:'. Do not name any files or results."),
    "report-failed": ("Write a short opening phrase for a task-failure "
                      "message, under 10 words, like 'That run failed:'. "
                      "Do not name the error."),
    "report-rejected": ("Write one short sentence saying the run was "
                       "left as-is and no live action was taken, under "
                       "15 words."),
    "report-cancelled": ("Write one short sentence saying the run was "
                        "stopped and no further steps ran, under 15 "
                        "words."),
}


def phrase_with_model(kind: str, say, timeout_s: float = 10.0) -> str | None:
    """One framing from a model, or None to fall back to rotation.

    ``say`` is a caller-supplied ``prompt -> text`` callable (the
    loop's respond-phase chat, or the handler's router chat), so this
    module never owns a model client. The call runs in a daemon thread
    with a short join timeout: a slow or hung model must never stall
    an approval card or a completion message. The reply is cleaned to
    a single short line; anything empty, overlong, or unusable falls
    back to the rotation instead of being shown.
    """
    import threading

    prompt = PHRASE_PROMPTS.get(kind)
    if prompt is None or say is None:
        return None
    result: list[str] = []

    def _run():
        try:
            result.append(str(say(prompt)))
        except Exception:
            pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    if not result:
        return None
    line = ""
    for candidate in result[0].splitlines():
        candidate = candidate.strip().strip("\"'").strip()
        if candidate:
            line = candidate
            break
    if not line or len(line) > 160:
        return None
    return line


def report_text(status: str, files: list[str], error: str,
                run_id: str, stats_line: str, phrase: str | None = None,
                blocked: list[str] | None = None) -> str:
    """Full completion message for a run, in natural language.

    The stats line (elapsed, tokens) is kept verbatim as the trailing
    line, so the WebUI stats display keeps working; only the human
    sentence above it varies. ``phrase`` is a model-written framing
    when one was produced, else the rotation is used. Blocked
    actions, when any, render as their own section above the stats
    line, so the owner sees what still needs a human.
    """
    if status == "done":
        head = f"{phrase or report_done_opener(run_id)} {files_sentence(files)}"
    elif status == "rejected":
        head = phrase or report_rejected(run_id)
    elif status == "cancelled":
        head = phrase or report_cancelled(run_id)
    else:
        head = f"{phrase or report_failed_opener(run_id)} {error}"
    if blocked:
        lines = "\n".join(f"- {item}" for item in blocked)
        head = f"{head}\nBlocked actions (need a human):\n{lines}"
    return f"{head}\n{stats_line}" if stats_line else head
