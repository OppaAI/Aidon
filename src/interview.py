"""Interview to a verified spec, in chat, not in cards.

Doctrine, fixed:
- Aidon asks questions in chat, the user answers in chat.
  Questions are conversation turns, they have no clock,
  and they never go through the approval gate. The gate
  is for DECISIONS, chat is for CONVERSATION.
- The interview triggers from the chat message itself,
  on every surface. There is no separate guidance box,
  no separate trigger field. ``start_from_message`` is
  the entry point, whatever surface called it.
- The interview ends in a single frozen spec, which
  binds the planner. A spec is not a chat summary, it
  is the contract for the run: goal, deliverables,
  constraints, budget, deadline, recipients, files,
  notifications, and assumptions, with no open
  questions left in required fields.

Spec verification, by code, not by model assertion:
- goal is non-empty and not a placeholder,
- deliverables are non-empty, each a concrete outcome
  or file, not "TBD",
- no required field contains a placeholder like TBD,
  TODO, or "to be decided",
- budget, if present, is a non-negative integer in cents,
- open_questions is empty for a verified spec.
Source verification, prices and facts checked against
sources, comes with the research part, not here. Here,
"verified" means complete, unambiguous, and approved:
the spec passes the checks above AND the human approved
it through the approval gate, which is a decision card
with a fail-closed clock, unlike the questions.

Q&A recording:
Every question and answer is recorded as STM (chat
history) and as episodic memory, scoped by project_id,
so a later run can see what was asked and answered,
and a correction updates the spec, it does not start
a new interview. The spec itself is saved as JSON
under <home>/specs/<project_id>.json, and recorded
as an episodic point, so recall can find it by text.

Model shape, for tests and for real runs:
The model is asked, in JSON, for either a next question
or a completed spec:
  {"question": "...", "spec": {...}}
  {"question": null, "spec": {...}, "complete": true}
A FakeClient in tests returns those JSON objects in
sequence. A real model that returns plain text is
treated as a question, not as a spec, so the interview
does not end on a non-JSON reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .model_client import Message, ModelClient

PLACEHOLDERS = ("tbd", "todo", "to be decided", "unknown", "n/a")


@dataclass
class Spec:
    goal: str = ""
    deliverables: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    budget_cents: int | None = None
    deadline: str = ""
    recipients: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "deliverables": self.deliverables,
            "constraints": self.constraints,
            "budget_cents": self.budget_cents,
            "deadline": self.deadline,
            "recipients": self.recipients,
            "files": self.files,
            "notifications": self.notifications,
            "assumptions": self.assumptions,
            "open_questions": self.open_questions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        if not isinstance(data, dict):
            return cls()
        def as_list(value):
            if isinstance(value, list):
                return [str(v) for v in value]
            return [str(value)] if value else []
        budget = data.get("budget_cents")
        try:
            budget = int(budget) if budget is not None else None
        except (TypeError, ValueError):
            budget = None
        return cls(
            goal=str(data.get("goal", "")),
            deliverables=as_list(data.get("deliverables", [])),
            constraints=as_list(data.get("constraints", [])),
            budget_cents=budget,
            deadline=str(data.get("deadline", "")),
            recipients=as_list(data.get("recipients", [])),
            files=as_list(data.get("files", [])),
            notifications=as_list(data.get("notifications", [])),
            assumptions=as_list(data.get("assumptions", [])),
            open_questions=as_list(data.get("open_questions", [])))

    def verify(self) -> tuple[bool, list[str]]:
        """Code checks for a complete, unambiguous spec."""
        failures: list[str] = []
        if not self.goal.strip() or self.goal.strip().lower() in PLACEHOLDERS:
            failures.append("goal is missing or a placeholder")
        if not self.deliverables:
            failures.append("deliverables are missing")
        for item in self.deliverables:
            if item.strip().lower() in PLACEHOLDERS:
                failures.append(f"deliverable is a placeholder: {item}")
        if self.budget_cents is not None and self.budget_cents < 0:
            failures.append("budget_cents is negative")
        if self.open_questions:
            failures.append(f"open questions remain: {', '.join(self.open_questions)}")
        for field_name, values in (("constraints", self.constraints),
                                   ("files", self.files)):
            for item in values:
                if item.strip().lower() in PLACEHOLDERS:
                    failures.append(f"{field_name} contains a placeholder: {item}")
        return (not failures, failures)


@dataclass
class InterviewResult:
    spec: Spec
    approved: bool
    verified: bool
    failures: list[str] = field(default_factory=list)
    exchanges: list[dict] = field(default_factory=list)


class InterviewRunner:
    """Chat interview, triggered by a chat message."""

    def __init__(self, client: ModelClient, home: Path, memory=None,
                 gate=None, project_id: str = "default",
                 max_questions: int = 20):
        self.client = client
        self.home = Path(home)
        self.memory = memory
        self.gate = gate
        self.project_id = project_id
        self.max_questions = max_questions

    def start_from_message(self, message: str, ask, approve=None) -> InterviewResult:
        """Entry point: a chat message starts the interview.

        ``ask`` is a chat callback, question -> answer string,
        with no clock and no card. ``approve`` is optional;
        if given, it is used for the spec decision, otherwise
        the gate, if present, is used. Questions never go
        through either approval path.
        """
        spec = Spec(goal=message.strip())
        exchanges: list[dict] = []
        # Record the opening message as STM + episodic, so
        # the interview is in chat history from the start.
        self._record_exchange("user", message, exchanges)
        for _ in range(self.max_questions):
            reply = self.client.chat([
                Message("system",
                        "You are interviewing the user to build a frozen "
                        "spec. Reply as JSON with keys question and spec. "
                        "If the spec is complete, question is null and "
                        "complete is true. Ask one question at a time, "
                        "in chat, never assume an answer."),
                Message("user", json.dumps({
                    "goal": message, "spec": spec.to_dict(),
                    "exchanges": exchanges[-10:]})),
            ])
            try:
                data = json.loads(reply)
            except (json.JSONDecodeError, AttributeError):
                data = {"question": reply.strip(), "spec": spec.to_dict()}
            if isinstance(data, dict) and data.get("spec"):
                spec = Spec.from_dict(data["spec"])
                if not spec.goal.strip():
                    spec.goal = message.strip()
            question = data.get("question") if isinstance(data, dict) else None
            complete = bool(data.get("complete")) if isinstance(data, dict) else False
            if not question or complete:
                break
            answer = ask(question)
            self._record_exchange("assistant", question, exchanges)
            self._record_exchange("user", answer, exchanges)
            if answer.strip().lower() in ("done", "go ahead", "that's all", "no more"):
                break
        verified, failures = spec.verify()
        # Save the spec, even if unverified, so the user can
        # see what is missing, and the run can resume it.
        spec_dir = self.home / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / f"{self.project_id}.json").write_text(
            json.dumps(spec.to_dict(), indent=2), encoding="utf-8")
        if self.memory is not None:
            try:
                self.memory.record_episodic(
                    self.project_id,
                    f"Interview spec: {spec.goal}",
                    {"spec": spec.to_dict(), "deliverables": spec.deliverables,
                     "verified": verified})
            except Exception:
                pass
        approved = False
        if verified:
            if approve is not None:
                approved = bool(approve(spec))
            elif self.gate is not None:
                decision = self.gate.ask(
                    "spec", "Approve this spec?",
                    json.dumps(spec.to_dict(), indent=2))
                approved = decision.verdict == "approved"
        return InterviewResult(spec=spec, approved=approved,
                               verified=verified, failures=failures,
                               exchanges=exchanges)

    def _record_exchange(self, role: str, text: str, exchanges: list[dict]) -> None:
        exchanges.append({"role": role, "text": text})
        if self.memory is not None:
            try:
                self.memory.record_stm(
                    role, text, project_id=self.project_id,
                    conversation_id=self.project_id)
            except Exception:
                pass
