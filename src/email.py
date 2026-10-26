"""Email, old Aidon policy, draft first, never send on a whim.

Old keys, only: AIDON_OWNER_EMAIL, AIDON_CORRECTED_SUBJECT.
No new duplicate. Transport in old Aidon was Google
Workspace MCP, not SMTP, and that MCP connection is not
built here, so sending here is via an injected sender,
for tests and for a future MCP sender, never via SMTP
in this module.
Policy, exactly as old Aidon:
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


def make_draft(home: Path, to: str, subject: str, body: str,
               corrected_subject: bool = True) -> dict:
    home = Path(home)
    drafts = home / "email" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    sent = _load_sent(home)
    key = f"{to}|{subject}"
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    final_subject = subject
    if corrected_subject and key in sent and sent[key] != body_hash:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        final_subject = f"Corrected: [{stamp}] {subject}"
    draft_id = uuid.uuid4().hex[:12]
    path = drafts / f"{draft_id}.md"
    path.write_text(f"To: {to}\nSubject: {final_subject}\nOriginal-Subject: {subject}\n\n{body}\n", encoding="utf-8")
    return {"ok": True, "draft_id": draft_id, "subject": final_subject,
            "path": str(path)}


def send_draft(home: Path, draft: dict, owner_email: str = "",
               approve=None, sender=None) -> dict:
    to = ""
    original_subject = draft["subject"]
    body = ""
    try:
        text = Path(draft["path"]).read_text()
        for line in text.splitlines():
            if line.startswith("To: "):
                to = line[4:].strip()
            if line.startswith("Original-Subject: "):
                original_subject = line[len("Original-Subject: "):].strip()
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
        sent[f"{to}|{original_subject}"] = hashlib.sha256(body.encode()).hexdigest()
        log = _sent_log(home)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(sent), encoding="utf-8")
    return result
