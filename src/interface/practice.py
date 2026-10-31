"""Free-time practice: scheduled windows that run goals until a pass.

A practice schedule names a window — a date and/or days of
week plus a start and end time — and a list of goals (each
optionally naming a spec). While the window is open, Aidon
picks goals at random and runs each one exactly like a
usual run, so a successful run learns its workflow (DAG)
into memory like any other success. The session ends at
the first successful pass, or when the window closes,
whichever comes first; failed attempts are retried with a
fresh random pick until then. Every successful run gets a
practice report (WebUI, Slack, WhatsApp); a window that
closes with no pass gets a summary in the WebUI record
only, so a silent failure never looks like a pass.

Practice runs are unattended by definition — free time
means the owner is away — so a pre-approved schedule's
gate auto-approves: creating the schedule IS the owner's
approval for those goals in that window, and the report
says so. Pre-approval follows the standing doctrine,
refined for practice: sends go only to the owner's own
destinations (owner email, owner WhatsApp, owner Slack
channel) through tools whose destinations are forced in
code, and deletes/installs stay inside one specific
practice folder in the sandbox, which the sandbox
confines. Anything else destructive — an external send,
a publish, spending, a delete outside the folder, a
system install — is rejected immediately, not left
waiting 180s for a card nobody will answer, and the
session moves on to the next random goal. A schedule
with preapprove off stops for approval like any other
run, which is the expected behaviour for that setting,
not a bug.

Storage is one JSON file under the Aidon home
(practice.json), separate from the cron-style scheduler:
a practice window is a duration, not a moment, so it does
not fit a fire-once job. The serve daemon ticks the
schedules once a minute and starts at most one session
per schedule per day.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, time
from pathlib import Path

_PRACTICE_FILE = "practice.json"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MAX_SESSIONS_KEPT = 20

# Kinds a pre-approved practice gate never auto-approves.
# The gate is allowlist-based (see PracticeGate.ask): only
# "live" and "mandatory" can ever be approved, and only
# under their own rules. Everything else — an external
# send, a publish, spending, a delete outside the folder,
# an install, a plugin change, a recording, a desktop
# action, or any kind the gate does not recognise — is
# rejected. The set below names the known-sensitive kinds
# for the rejection reason; unknown kinds are rejected by
# the allowlist, not by this set.
PRACTICE_IRREVERSIBLE_KINDS = {
    "send_external", "external_email", "publish", "spend", "delete",
    "live_execute", "plugin", "install", "record",
}


class PracticeGate:
    """Auto-approve practice steps within the owner's allowance.

    Same ask() shape as ApprovalGate, so it drops into any
    GoalRunner. The gate is allowlist-based: only "live"
    and "mandatory" can ever be approved, and only under
    their own rules below. Every other kind — a recording,
    an install, a plugin change, a desktop action, an
    external send, a publish, spending, a delete, or any
    kind the gate does not recognise — is rejected at once,
    with the reason in the decision. That default-deny is
    safe here because a practice run legitimately produces
    only "live" and "mandatory": practice runners carry
    only the practice tools, and plugin tools do not route
    through this gate. A tool's force_ask flag does not
    change this: the live card's force_ask means "don't
    skip via standing policy", and this gate uses no
    standing policy — it uses the schedule grant — while
    a sensitive kind is rejected on its kind, force or not.

    The allowance the owner stated for practice:
    sends go only to the owner's own destinations (owner
    email, owner WhatsApp, owner Slack channel) through the
    practice tools, whose destinations are forced in code;
    deletes and installs stay inside one specific
    practice folder in the sandbox, which the sandbox
    confines. A rejected step fails fast instead of
    stalling on a card, and the session moves on to the
    next random goal.

    In particular:

    - "live" is approved only for a reversible plan (the
      loop certifies this, with the practice tools on the
      allowlist, so an owner-destination send does not
      trip the trigger-word check).
    - "mandatory" shell commands are approved only when
      the run is actually sandboxed AND the command shows
      no escape: no sudo, mkfs, raw disk writes, device
      writes, shutdown, firewall flush, chmod 777, or
      piped download-to-shell, and no absolute, home, or
      parent path — a practice delete uses a relative path
      inside the practice folder, or it does not run.
    """

    def __init__(self, practice_dir=None, sandboxed: bool = False):
        self.practice_dir = Path(practice_dir) if practice_dir else None
        self.sandboxed = bool(sandboxed)

    def ask(self, kind: str, title: str, body: str, **kwargs):
        from .approval import Decision
        if kind == "question":
            raise ValueError("questions are chat turns, not approval cards")
        if kind == "live":
            # A publish-class live card is the extra-tool send
            # path, not plan execution: never approved here,
            # whatever the plan's reversibility says.
            if str(kwargs.get("action_class") or "") == "publish":
                return Decision(
                    "rejected",
                    reason="practice pre-approval excludes publishing/sending")
            if bool(kwargs.get("reversible", False)):
                return Decision("approved",
                                reason="practice schedule pre-approval")
            return Decision(
                "rejected",
                reason="practice pre-approval needs a reversible plan")
        if kind == "mandatory":
            if self.sandboxed and _mandatory_safe_for_practice(body):
                return Decision("approved",
                                reason="practice folder shell command")
            return Decision(
                "rejected",
                reason="practice allows shell deletes/installs only "
                       "inside the practice folder, relative paths, "
                       "no sudo/system commands")
        return Decision(
            "rejected",
            reason=f"practice pre-approval does not cover {kind}")


# Mandatory patterns that are NEVER practice-safe, even in the
# sandbox: everything except the recursive delete, which the
# practice folder exists to allow. The recursive delete
# itself is gated by the path scan in
# _mandatory_safe_for_practice, not by this list.
_MANDATORY_ALWAYS_REJECT = (
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+.*\bof=",
    r"/dev/",
    r"\b(shutdown|reboot|poweroff)\b",
    r"\biptables\s+-F\b",
    r"\bchmod\s+777\b",
    r"(curl|wget)\b[^|\n]*\|\s*(sh|bash)\b",
    r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;",
)


def _mandatory_safe_for_practice(body: str) -> bool:
    """True when a mandatory shell command stays in the practice folder.

    The body carries the command text ("Run shell command:
    ...?"). Any system-level pattern rejects, and any path
    that is absolute, home-relative, or parent-relative
    rejects: from the host side the gate cannot prove an
    absolute path lands inside the practice folder, so a
    practice delete must use a relative path. Fail closed
    on anything unparseable.
    """
    import re as _re
    text = body or ""
    for pattern in _MANDATORY_ALWAYS_REJECT:
        if _re.search(pattern, text):
            return False
    for token in _re.split(r"[\s\"'`,;]+", text):
        if not token:
            continue
        if token.startswith("~") or token.startswith("/"):
            return False
        if ".." in token.split("/"):
            return False
    return True


PRACTICE_TOOL_NAMES = ("practice_whatsapp", "practice_slack", "practice_email")


def _practice_tool_text(args, kwargs) -> str:
    for key in ("text", "message", "body", "content"):
        value = kwargs.get(key)
        if value:
            return str(value)
    if args:
        return str(args[-1])
    return ""


def _practice_tool_file(args, kwargs) -> str:
    for key in ("file", "path", "filename", "attachment", "filepath"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return ""


def practice_tools(practice_dir, msg_config, owner_email: str = "",
                   gmail_client_id: str = "", gmail_client_secret: str = "",
                   gmail_refresh_token: str = "", senders: dict | None = None):
    """Owner-only messaging tools for practice runs.

    Three tools, one per owner destination, and the
    destination is forced in code — the planner supplies
    only the message and an optional file, never a
    recipient, so a practice run cannot message anyone but
    the owner. Files are confined to the practice folder
    before sending. The names carry no post/send/publish
    substring, so the extra-tool dispatch does not gate
    them; their safety is the forced destination plus the
    confinement, and the plan-reversibility allowlist
    (PRACTICE_TOOL_NAMES) covers the trigger-word check.
    senders overrides the low-level send callables for
    tests: whatsapp_text, whatsapp_media, slack_text,
    slack_file, gmail_post, gmail_send.
    """
    import base64
    import mimetypes
    from email.message import EmailMessage

    senders = senders or {}
    root = Path(practice_dir).resolve()

    def _confined(name: str) -> Path:
        relative = str(name).replace("\\", "/")
        if relative.startswith("workdir/"):
            relative = relative[len("workdir/"):]
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path outside practice folder: {name}")
        return target

    def _wa_text(to, text):
        fn = senders.get("whatsapp_text")
        if fn is not None:
            return fn(msg_config, to, text)
        from .messaging import send_whatsapp
        return send_whatsapp(msg_config, to, text)

    def _wa_media(to, path, caption):
        fn = senders.get("whatsapp_media")
        if fn is not None:
            return fn(msg_config, to, path, caption)
        from .messaging import send_whatsapp_media
        return send_whatsapp_media(msg_config, to, path, caption)

    def _sl_text(channel, text):
        fn = senders.get("slack_text")
        if fn is not None:
            return fn(msg_config, channel, text)
        from .messaging import send_slack
        return send_slack(msg_config, channel, text)

    def _sl_file(channel, path, title):
        fn = senders.get("slack_file")
        if fn is not None:
            return fn(msg_config, channel, path, title)
        from .messaging import send_slack_file
        return send_slack_file(msg_config, channel, path, title)

    def practice_whatsapp(*args, **kwargs):
        owner = getattr(msg_config, "whatsapp_owner", "")
        if not owner:
            return "practice whatsapp failed: AIDON_WHATSAPP_OWNER is not configured"
        text = _practice_tool_text(args, kwargs)
        name = _practice_tool_file(args, kwargs)
        try:
            if name:
                target = _confined(name)
                if not target.is_file():
                    return f"practice whatsapp failed: not found in practice folder: {name}"
                result = _wa_media(owner, target, text)
            else:
                result = _wa_text(owner, text)
        except Exception as exc:
            return f"practice whatsapp failed: {type(exc).__name__}: {exc}"
        if result.get("ok"):
            return f"practice whatsapp sent to owner (id {result.get('message_id', '')})"
        return f"practice whatsapp failed: {result.get('error', 'unknown error')}"

    def practice_slack(*args, **kwargs):
        channel = getattr(msg_config, "slack_outbound_channel", "")
        if not channel:
            return "practice slack failed: AIDON_SLACK_OUTBOUND_CHANNEL is not configured"
        text = _practice_tool_text(args, kwargs)
        name = _practice_tool_file(args, kwargs)
        try:
            if name:
                target = _confined(name)
                if not target.is_file():
                    return f"practice slack failed: not found in practice folder: {name}"
                result = _sl_file(channel, target, text)
            else:
                result = _sl_text(channel, text)
        except Exception as exc:
            return f"practice slack failed: {type(exc).__name__}: {exc}"
        if result.get("ok"):
            return f"practice slack sent to owner channel (file {result.get('file_id', '')})" if name else "practice slack sent to owner channel"
        return f"practice slack failed: {result.get('error', 'unknown error')}"

    def practice_email(*args, **kwargs):
        if not owner_email:
            return "practice email failed: AIDON_OWNER_EMAIL is not configured"
        if not (gmail_client_id and gmail_client_secret and gmail_refresh_token):
            return "practice email failed: Gmail OAuth keys are not configured"
        subject = str(kwargs.get("subject", "") or "Aidon practice message")
        body = _practice_tool_text(args, kwargs)
        name = _practice_tool_file(args, kwargs)
        try:
            from ..capabilities.gmail import refresh_access_token
            gmail_post = senders.get("gmail_post")
            token = refresh_access_token(
                gmail_client_id, gmail_client_secret, gmail_refresh_token,
                post=gmail_post)
            message = EmailMessage()
            message["To"] = owner_email
            message["Subject"] = subject
            message.set_content(body)
            if name:
                target = _confined(name)
                if not target.is_file():
                    return f"practice email failed: not found in practice folder: {name}"
                data = target.read_bytes()
                maintype, subtype = (mimetypes.guess_type(target.name)[0]
                                     or "application/octet-stream").split("/", 1)
                message.add_attachment(data, maintype=maintype, subtype=subtype,
                                       filename=target.name)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            gmail_send = senders.get("gmail_send")
            if gmail_send is not None:
                result = gmail_send(token, {"raw": raw})
            else:
                import json as _json
                import urllib.request as _url
                req = _url.Request(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    data=_json.dumps({"raw": raw}).encode(), method="POST",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                with _url.urlopen(req, timeout=15) as response:
                    result = _json.loads(response.read().decode())
        except Exception as exc:
            return f"practice email failed: {type(exc).__name__}: {exc}"
        if isinstance(result, dict) and result.get("id"):
            return f"practice email sent to owner (id {result['id']})"
        return f"practice email failed: {result}"

    return {"practice_whatsapp": practice_whatsapp,
            "practice_slack": practice_slack,
            "practice_email": practice_email}


from .practice_schedule import (
    WEEKDAY_NAMES, add_schedule, coerce_preapprove, due_for_session,
    get_schedule, load_schedules, load_spec, practice_report_text,
    record_session, remove_schedule, run_practice_session, save_schedules,
    update_live, validate_schedule, window_active, window_end,
)
