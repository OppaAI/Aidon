"""Model verifier, using Spark, second opinion, never the primary.

Code verification is primary in Aidon: a deliverable
passes only if it exists, is non-empty, and contains
what the criteria require, checked by code, never by a
model's word. The model verifier is a second pass,
after code verification passes, using the same Spark
model as the planner, not a different model, by owner
decision. A different model would reduce self-grading
bias, but it is not required, and it would cost a
second model setup for a check that code already does.
The model pass now receives the same criteria the code
pass used, plus a definition of complete, so it judges
correctness and completeness instead of a loose goal
match: every criterion met by the shown content, no
placeholders, substantive content rather than a
restatement of the goal, figures consistent with the
goal. Truncation is disclosed to the model, and a
criterion that cannot be confirmed from the shown
content is a NO, not an assumed pass — for text and
extracted content. Binary deliverables are different:
a PDF or office document shows extracted text; an image
is shown to the model as vision; anything else binary —
archives, executables, encrypted files — is opaque and
is judged on properties only (exists, size, format).
Opacity is never a failure: a criterion about an opaque
file that cannot be confirmed from its properties is an
UNKNOWN, not a NO, and when every deliverable is opaque
the model is not called at all — code verification
decides alone. An encrypted file is opaque by design:
the verifier never holds its key and its plaintext never
reaches a model; the container is verified by properties
and by a code round-trip check, never by reading content.
Verdict: "yes" means the model says the deliverables
match the goal, "no" means it says they do not, with
a reason, "unknown" means the model reply was empty,
did not start with YES or NO, or concerned only opaque
files, and then code verification decides alone, never
a failure on an unparseable reply. A "no" triggers
repair, same as a code failure, it does not go to approval.
"""

from __future__ import annotations

from pathlib import Path

from ..system.model_client import Message, ModelClient, image_message
from .verify import read_deliverable, resolve_in_workdir

# Images the model can see, by suffix. Anything else binary
# is opaque: archives, executables, encrypted containers.
# The size cap keeps a huge image out of the model payload;
# above it, the image is opaque like any other binary.
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg"}
_IMAGE_MAX_BYTES = 5 * 1024 * 1024


class ModelVerifier:
    def __init__(self, client: ModelClient):
        self.client = client
        self.effort_escalated = False

    def verify(self, goal: str, deliverables: list[str], workdir: Path,
               criteria: list[str] | None = None) -> dict:
        workdir = Path(workdir)
        files_text = []
        images: list[tuple[bytes, str]] = []
        judgeable = 0
        opaque = 0
        absent = 0
        for name in deliverables:
            path = resolve_in_workdir(workdir, name)
            if path is None:
                files_text.append(f"--- {name} ---\n(outside workdir, not read)")
                absent += 1
                continue
            if not path.is_file():
                files_text.append(f"--- {name} ---\n(missing)")
                absent += 1
                continue
            info = read_deliverable(path)
            size = info["size"]
            if info["kind"] == "opaque":
                mime = _IMAGE_TYPES.get(path.suffix.lower())
                if mime is not None and size <= _IMAGE_MAX_BYTES:
                    try:
                        data = path.read_bytes()
                    except OSError:
                        files_text.append(
                            f"--- {name} ({size} bytes, {mime}) ---\n"
                            "[opaque: content not readable; "
                            "judge from properties only]")
                        opaque += 1
                        continue
                    images.append((data, mime))
                    files_text.append(
                        f"--- {name} ({size} bytes, {mime}) ---\n"
                        "[image shown to the model; judge its visible content]")
                    judgeable += 1
                    continue
                files_text.append(
                    f"--- {name} ({size} bytes, "
                    f"{path.suffix.lower() or 'binary'} format) ---\n"
                    "[opaque: content not readable as text; "
                    "judge from properties only]")
                opaque += 1
                continue
            label = "extracted text" if info["kind"] == "extracted" else "text"
            files_text.append(f"--- {name} ({size} bytes, {label}) ---\n{info['shown']}")
            judgeable += 1
        if judgeable == 0 and opaque > 0 and absent == 0:
            # Nothing here can be judged on content — every
            # deliverable is opaque, none missing or escaping.
            # Calling the model would only produce a NO from
            # unreadability, which is exactly the failure this
            # path exists to prevent. Code verification, which
            # already passed to reach this point, decides alone.
            # Missing or escaping names are different: absence
            # is a genuine failure, so they still go to the
            # model with their markers, as before.
            return {"verdict": "unknown",
                    "reason": "no judgeable deliverables; code verification decides"}
        if criteria:
            criteria_text = "Criteria (every one must be met by the shown content):\n" + "\n".join(
                f"- {c}" for c in criteria)
        else:
            criteria_text = ("Criteria: none stated; judge against the goal.")
        prompt = (f"Goal: {goal}\n{criteria_text}\n\n"
                  + "\n\n".join(files_text)
                  + "\n\nComplete means: every criterion is met by the "
                    "shown content; no placeholders (TODO, TBD, lorem "
                    "ipsum, empty sections, \"to be added\"); content is "
                    "substantive, not a restatement of the goal; figures, "
                    "where present, are consistent with the goal. Files "
                    "marked [opaque] are intentionally unreadable "
                    "(archives, executables, encrypted files): judge "
                    "them from properties only — existence, size, "
                    "format. Never answer NO solely because an opaque "
                    "file's content cannot be confirmed; if the only "
                    "unconfirmed criteria concern opaque files, answer "
                    "UNKNOWN. If a shown (text, extracted, or image) "
                    "file is truncated and a criterion cannot be "
                    "confirmed from the shown content, answer NO "
                    "and name that criterion. When you answer NO, name the deliverable file(s) that fail in your one-line reason, so only those files are repaired and files that passed are kept."
                    "\n\nAnswer YES if every deliverable is complete and "
                    "matches the goal and criteria, otherwise NO, with one "
                    "line reason — or UNKNOWN when the only unconfirmed "
                    "criteria concern opaque files. Start your answer "
                    "with YES, NO, or UNKNOWN.")
        from .think_effort import sampling_for
        sampling = sampling_for("verify", escalate=self.effort_escalated)
        messages = [Message("system", "You verify deliverables against "
                                      "explicit criteria, strictly. Judge "
                                      "only what is shown; never assume "
                                      "unseen content is fine."),
                    image_message(prompt, images)
                    if images else Message("user", prompt)]
        if hasattr(self.client, "chat_phase"):
            try:
                reply = self.client.chat_phase("verify", messages,
                                               sampling=sampling).strip()
            except TypeError:
                reply = self.client.chat_phase("verify", messages).strip()
        else:
            try:
                reply = self.client.chat(messages, sampling=sampling).strip()
            except TypeError:
                reply = self.client.chat(messages).strip()
        if not reply:
            return {"verdict": "unknown", "reason": "empty model reply"}
        first = reply.split()[0].strip(".,:").upper()
        if first == "YES":
            return {"verdict": "yes", "reason": reply[:300]}
        if first == "NO":
            return {"verdict": "no", "reason": reply[:300]}
        return {"verdict": "unknown", "reason": reply[:300]}
