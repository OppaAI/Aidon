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
content is a NO, not an assumed pass.
Verdict: "yes" means the model says the deliverables
match the goal, "no" means it says they do not, with
a reason, "unknown" means the model reply was empty
or did not start with YES or NO, and then code
verification decides alone, never a failure on an
unparseable reply. A "no" triggers repair, same as a
code failure, it does not go to approval.
"""

from __future__ import annotations

from pathlib import Path

from ..system.model_client import Message, ModelClient


class ModelVerifier:
    def __init__(self, client: ModelClient):
        self.client = client
        self.effort_escalated = False

    def verify(self, goal: str, deliverables: list[str], workdir: Path,
               criteria: list[str] | None = None) -> dict:
        workdir = Path(workdir)
        files_text = []
        for name in deliverables:
            path = workdir / name
            if path.is_file():
                try:
                    size = path.stat().st_size
                    full = path.read_text()
                    shown = full[:2000]
                    if len(full) > 2000:
                        shown += "\n[content truncated to 2000 chars]"
                    files_text.append(f"--- {name} ({size} bytes) ---\n{shown}")
                except Exception:
                    files_text.append(f"--- {name} ---\n(unreadable)")
            else:
                files_text.append(f"--- {name} ---\n(missing)")
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
                    "where present, are consistent with the goal. For "
                    "binary or formatted files, judge from structure and "
                    "visible text. If a file is truncated and a criterion "
                    "cannot be confirmed from the shown content, answer NO "
                    "and name that criterion."
                    "\n\nAnswer YES if every deliverable is complete and "
                    "matches the goal and criteria, otherwise NO, with one "
                    "line reason. Start your answer with YES or NO.")
        from .think_effort import sampling_for
        sampling = sampling_for("verify", escalate=self.effort_escalated)
        messages = [Message("system", "You verify deliverables against "
                                      "explicit criteria, strictly. Judge "
                                      "only what is shown; never assume "
                                      "unseen content is fine."),
                    Message("user", prompt)]
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
