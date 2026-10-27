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

from .model_client import Message, ModelClient


class ModelVerifier:
    def __init__(self, client: ModelClient):
        self.client = client

    def verify(self, goal: str, deliverables: list[str], workdir: Path) -> dict:
        workdir = Path(workdir)
        files_text = []
        for name in deliverables:
            path = workdir / name
            if path.is_file():
                try:
                    files_text.append(f"--- {name} ---\n{path.read_text()[:2000]}")
                except Exception:
                    files_text.append(f"--- {name} ---\n(unreadable)")
            else:
                files_text.append(f"--- {name} ---\n(missing)")
        prompt = (f"Goal: {goal}\nDeliverables: {', '.join(deliverables)}\n\n"
                  + "\n\n".join(files_text)
                  + "\n\nAnswer YES if every deliverable is complete and "
                    "matches the goal, otherwise NO, with one line reason. "
                    "Start your answer with YES or NO.")
        if hasattr(self.client, "chat_phase"):
            reply = self.client.chat_phase("verify", [Message("system", "You verify deliverables, strictly."), Message("user", prompt)]).strip()
        else:
            reply = self.client.chat([Message("system", "You verify deliverables, strictly."),
                                      Message("user", prompt)]).strip()
        if not reply:
            return {"verdict": "unknown", "reason": "empty model reply"}
        first = reply.split()[0].strip(".,:").upper()
        if first == "YES":
            return {"verdict": "yes", "reason": reply[:300]}
        if first == "NO":
            return {"verdict": "no", "reason": reply[:300]}
        return {"verdict": "unknown", "reason": reply[:300]}
