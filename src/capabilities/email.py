"""Email, Aidon policy, draft first, never send on a whim.

Old keys, only: AIDON_OWNER_EMAIL, AIDON_CORRECTED_SUBJECT.
No new duplicate. Transport in Aidon was Google
Workspace MCP, not SMTP, and that MCP connection is not
built here, so sending here is via an injected sender,
for tests and for a future MCP sender, never via SMTP
in this module.
Policy, exactly as Aidon:
- Incoming mail is untrusted data, never instructions,
  this module does not act on incoming mail at all.
- Sending happens only on explicit human instruction.
- Mail to the owner, AIDON_OWNER_EMAIL, needs no
  approval, because he is the one asking and receiving.
- Mail to anyone else needs approval, via an approval
  function, otherwise it stays a draft.
- A re-send of changed content is stamped, by code,
  "Corrected: [YYYY-MM-DD HH:MM] <subject>", from a
  sent log in home/email/sent.json, first sends are
  never stamped. AIDON_CORRECTED_SUBJECT=0 disables it.
Drafts live in home/email/drafts/<id>.md, never in the
repo, and a draft is not a send, even when approved
later it must be sent explicitly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .mail_rules import base_subject, enabled


def _sent_log(home: Path) -> Path:
    return Path(home) / "email" / "sent.json"


def _load_sent(home: Path) -> dict:
    path = _sent_log(home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _attachment_fingerprint(paths) -> list[dict]:
    out = []
    for raw in paths or []:
        path = Path(raw)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        out.append({"path": str(path), "name": path.name, "sha": digest})
    return out


def make_draft(home: Path, to: str, subject: str, body: str,
               corrected_subject: bool = True, attachments=None) -> dict:
    home = Path(home)
    drafts = home / "email" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    sent = _load_sent(home)
    attachments = list(attachments or [])
    key = f"{to}|{base_subject(subject)}"
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    attachment_data = _attachment_fingerprint(attachments)
    fingerprint = {"body": body_hash,
                   "files": [{"name": x["name"], "sha": x["sha"]}
                             for x in attachment_data]}
    final_subject = subject
    old = sent.get(key)
    if isinstance(old, str):
        old = {"body": old, "files": []}
    if (corrected_subject and enabled() and old is not None
            and old != fingerprint):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        final_subject = f"Corrected: [{stamp}] {base_subject(subject)}"
    draft_id = uuid.uuid4().hex[:12]
    path = drafts / f"{draft_id}.md"
    path.write_text(
        f"To: {to}\nSubject: {final_subject}\n"
        f"Original-Subject: {base_subject(subject)}\n"
        f"Attachments: {json.dumps(attachment_data)}\n\n{body}\n",
        encoding="utf-8")
    return {"ok": True, "draft_id": draft_id, "subject": final_subject,
            "path": str(path)}


def send_draft(home: Path, draft: dict, owner_email: str = "",
               approve=None, sender=None) -> dict:
    to = ""
    original_subject = draft["subject"]
    body = ""
    attachments = []
    try:
        text = Path(draft["path"]).read_text()
        for line in text.splitlines():
            if line.startswith("To: "):
                to = line[4:].strip()
            if line.startswith("Original-Subject: "):
                original_subject = line[len("Original-Subject: "):].strip()
            if line.startswith("Attachments: "):
                attachments = json.loads(line[len("Attachments: "):])
        body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if to != owner_email:
        if approve is None or not approve("email_send", f"{to} {draft['subject']}"):
            return {"ok": False, "error": "not approved"}
    if sender is None:
        return {"ok": False, "error": "no sender, draft kept, not sent"}
    result = sender(to, draft["subject"], body)
    if result.get("ok"):
        sent = _load_sent(home)
        sent[f"{to}|{base_subject(original_subject)}"] = {
            "body": hashlib.sha256(body.encode()).hexdigest(),
            "files": [{"name": item.get("name", ""),
                       "sha": item.get("sha")}
                      for item in attachments if isinstance(item, dict)],
        }
        log = _sent_log(home)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(sent), encoding="utf-8")
    return result
