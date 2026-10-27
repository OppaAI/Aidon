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

from ..system.model_client import Message, ModelClient

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


def spec_sheet(spec: Spec) -> str:
    """Readable spec sheet for the approval card, not raw JSON.

    The card body is this sheet: goal, deliverables,
    constraints, budget in dollars, deadline, recipients,
    files, notifications, assumptions, and open questions.
    A human approves a sheet they can read, not a JSON dump.
    """
    def lines(title: str, values: list[str]) -> list[str]:
        if not values:
            return [f"{title}: -"]
        out = [f"{title}:"]
        out.extend(f"  - {value}" for value in values)
        return out
    budget = "-"
    if spec.budget_cents is not None:
        budget = f"${spec.budget_cents / 100:.2f} ({spec.budget_cents} cents)"
    sheet = [f"Goal: {spec.goal}", f"Budget: {budget}",
             f"Deadline: {spec.deadline or '-'}"]
    sheet.extend(lines("Deliverables", spec.deliverables))
    sheet.extend(lines("Constraints", spec.constraints))
    sheet.extend(lines("Recipients", spec.recipients))
    sheet.extend(lines("Files", spec.files))
    sheet.extend(lines("Notifications", spec.notifications))
    sheet.extend(lines("Assumptions", spec.assumptions))
    sheet.extend(lines("Open questions", spec.open_questions))
    return "\n".join(sheet)


def spec_sheet_md(spec: Spec) -> str:
    """Standard format spec sheet, Markdown, human readable.

    This is THE spec sheet format: Markdown headings and
    bullet lists, meant to be read by a human in an editor
    or in the chat box, not a JSON dump. It is the file
    users write themselves, copy, edit, and attach in
    chat, and it is the body of the spec sheet approval
    card. JSON is only the machine copy stored alongside
    it in specs/. The header points to the template and
    to the docs, so a user who finds a sheet in specs/
    knows they can write their own.
    """
    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {v}" for v in values) if values else "None"
    budget = "None"
    if spec.budget_cents is not None:
        budget = f"${spec.budget_cents / 100:.2f} ({spec.budget_cents} cents)"
    return "\n".join([
        "# Spec Sheet",
        "",
        "How to use this sheet: fill it in, save it, and attach it in the chat box. "
        "Aidon reads it, verifies it, and skips the interview. "
        "Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo. "
        "Docs: README, Spec sheet section.",
        "",
        f"## Goal",
        "",
        spec.goal or "None",
        "",
        "## Deliverables",
        "",
        bullets(spec.deliverables),
        "",
        "## Constraints",
        "",
        bullets(spec.constraints),
        "",
        f"## Budget",
        "",
        budget,
        "",
        f"## Deadline",
        "",
        spec.deadline or "No deadline",
        "",
        "## Recipients",
        "",
        bullets(spec.recipients),
        "",
        "## Files",
        "",
        bullets(spec.files),
        "",
        "## Notifications",
        "",
        bullets(spec.notifications),
        "",
        "## Assumptions",
        "",
        bullets(spec.assumptions),
        "",
        "## Open questions",
        "",
        bullets(spec.open_questions),
        "",
    ])


SPEC_SHEET_TEMPLATE = """# Spec Sheet

How to use this sheet: fill it in, save it, and attach it in the chat box.
Aidon reads it, verifies it, and skips the interview.
Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Example: examples/SPEC-SHEET-EXAMPLE.md in the repo.
Docs: README, Spec sheet section.

## Goal

Write the goal in plain language, in one sentence, saying what you want Aidon to do and for whom, not how it will do it.

## Deliverables

List each file or concrete outcome Aidon must produce, one per bullet, with the file name if it is a file.

## Constraints

List every hard limit Aidon must not break, one per bullet, such as a date range, a format, or a source file. Write None if there are no hard limits.

## Budget

Write the maximum total amount Aidon may spend, in dollars and cents, or write None if there is no budget cap. Do not write a budget here unless it is a real limit.

## Deadline

Write the date and time the work must be finished by, or write No deadline if there is none.

## Recipients

List who the work is for or who should receive the output, with their contact details if Aidon must send it to them, or write None if there is no recipient.

## Files

List every input file Aidon should read and every output file it should create, one per bullet, with the file name and path.

## Notifications

Write how you want to be told when the work is done or if it fails, or write None if no notification is needed.

## Assumptions

List anything Aidon may assume without asking, one per bullet, or write None if there are no assumptions.

## Open questions

List any questions that are still open, one per bullet, or write None if the spec is complete. A spec with open questions is not verified.
"""


def _parse_budget(text: str) -> int | None:
    import re
    text = text.strip()
    if not text or text == "-":
        return None
    match = re.search(r"(\d+)\s*cents", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", text)
    if match:
        return int(round(float(match.group(1)) * 100))
    return None


def parse_spec_sheet(text: str) -> Spec:
    """Parse a user-written spec sheet back into a Spec.

    Accepts JSON, the plain spec_sheet() format, and the
    Markdown standard format, so a user can write a sheet
    in an editor, in chat, or by copying the template,
    and Aidon can read it without an interview. Unknown
    lines are ignored, never invented into fields.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return Spec.from_dict(json.loads(stripped))
        except (json.JSONDecodeError, TypeError):
            pass
    spec = Spec()
    section = ""
    sections = {
        "goal": "goal", "deliverables": "deliverables",
        "constraints": "constraints", "budget": "budget",
        "deadline": "deadline", "recipients": "recipients",
        "files": "files", "notifications": "notifications",
        "assumptions": "assumptions", "open questions": "open_questions",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in ("none", "no deadline", "-"):
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if heading in sections and heading != "spec sheet":
                section = sections[heading]
            continue
        if ":" in line and not line.startswith(("-", "*")):
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in sections:
                section = sections[key]
                value = value.strip()
                if not value or value == "-":
                    continue
                if section == "goal":
                    spec.goal = value
                elif section == "budget":
                    spec.budget_cents = _parse_budget(value)
                elif section == "deadline":
                    spec.deadline = value
                else:
                    getattr(spec, section).append(value)
                continue
        if line.startswith(("-", "*")):
            item = line.lstrip("-*").strip()
            if not item or item == "-" or item.lower() == "none":
                continue
            if section == "goal":
                spec.goal = item
            elif section in ("deliverables", "constraints", "recipients",
                             "files", "notifications", "assumptions",
                             "open_questions"):
                getattr(spec, section).append(item)
            continue
        # A bare line under Goal, Budget, or Deadline is that field.
        if section == "goal" and not spec.goal and line != "-":
            spec.goal = line
        elif section == "budget" and spec.budget_cents is None:
            spec.budget_cents = _parse_budget(line)
        elif section == "deadline" and not spec.deadline and line != "-":
            spec.deadline = line
    return spec


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
        # Build from memory and previous runs first, before
        # asking anything new. This is what keeps the
        # interview from re-asking what is already known:
        # - previous conversation in this project,
        # - previous episodic specs for a similar goal,
        # - previous successful Experiences for a similar goal.
        # A prefilled field is a proposal, not a fact: the
        # user can correct it in chat, and the spec sheet
        # card at the end is where they approve or reject it.
        memory_context: dict = {}
        initial_spec = Spec(goal=message.strip())
        if self.memory is not None:
            try:
                context = self.memory.conversation_context(self.project_id)
                memory_context["previous_exchanges"] = [
                    {"role": p.payload.get("role", ""), "text": p.text}
                    for p in context["exchanges"][-5:]]
                if context["summary"]:
                    memory_context["previous_summary"] = context["summary"]
                # Previous specs: episodic points carrying a spec.
                for point in self.memory.list_points("episodic", limit=100):
                    saved = point.payload.get("spec")
                    if isinstance(saved, dict) and saved.get("goal"):
                        previous = Spec.from_dict(saved)
                        spec = self._prefill(spec, previous)
                        memory_context["previous_spec"] = previous.to_dict()
                        break
                # Previous runs: Experience deliverables and lesson.
                try:
                    from ..memory.experience import recall_experiences
                    experiences = recall_experiences(self.memory, message, limit=1)
                    if experiences:
                        exp = experiences[0]
                        if not spec.deliverables:
                            spec.deliverables = [str(n.get("deliverable", ""))
                                                 for n in exp.dag_nodes
                                                 if n.get("deliverable")]
                        if exp.lesson and exp.lesson not in spec.assumptions:
                            spec.assumptions.append(exp.lesson)
                        memory_context["previous_experience"] = exp.to_text()
                except Exception:
                    pass
            except Exception:
                pass
        initial_spec = Spec.from_dict(spec.to_dict())
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
                    "exchanges": exchanges[-10:],
                    "from_memory": memory_context})),
            ])
            try:
                data = json.loads(reply)
            except (json.JSONDecodeError, AttributeError):
                data = {"question": reply.strip(), "spec": spec.to_dict()}
            if isinstance(data, dict) and data.get("spec"):
                spec = Spec.from_dict(data["spec"])
                if not spec.goal.strip():
                    spec.goal = message.strip()
                # Spec built from questions + memory: if the
                # model omitted a field that memory already
                # knew, keep the memory value, do not lose it.
                spec = self._prefill(spec, initial_spec)
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
        self._save_spec(spec)
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
                # Spec sheet card, not a JSON dump: the user
                # approves a sheet they can read, before any
                # planning or live step proceeds.
                decision = self.gate.ask(
                    "spec", "Approve this spec sheet?",
                    spec_sheet_md(spec),
                    force_ask=True,
                )
                approved = decision.verdict in ("approved", "auto_approved")
        return InterviewResult(spec=spec, approved=approved,
                               verified=verified, failures=failures,
                               exchanges=exchanges)

    def ensure_template(self) -> Path:
        """Template spec sheet in the specs/ path, if not already there.

        Users find it at <home>/specs/template.md, copy it,
        fill it in, and attach it in chat. It is not
        overwritten if the user already edited it.
        """
        spec_dir = self.home / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        target = spec_dir / "template.md"
        if not target.exists():
            target.write_text(SPEC_SHEET_TEMPLATE, encoding="utf-8")
        return target

    def _save_spec(self, spec: Spec) -> tuple[Path, Path]:
        """Save a spec as JSON and as a standard Markdown sheet.

        Both live in <home>/specs/: <project>.json for code,
        <project>.md for humans. The template is ensured
        alongside them, so a user who finds a sheet knows
        they can write their own. Even an unverified spec
        is saved, so the user can see what is missing and
        resume it.
        """
        spec_dir = self.home / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_template()
        json_path = spec_dir / f"{self.project_id}.json"
        md_path = spec_dir / f"{self.project_id}.md"
        json_path.write_text(json.dumps(spec.to_dict(), indent=2),
                             encoding="utf-8")
        md_path.write_text(spec_sheet_md(spec), encoding="utf-8")
        return json_path, md_path

    def load_attached_spec(self, sheet_text: str, approve=None) -> InterviewResult:
        """Attach a spec sheet in chat, skip the interview.

        The user writes their own sheet, from the template
        or by hand, and attaches it in the chat box. Aidon
        parses it, verifies it by code, saves it as JSON
        and Markdown in specs/, records it as episodic,
        and confirms it with a spec sheet card. No
        questions are asked, the interview step is skipped.
        If the sheet is unverified, it is not approved,
        and the run does not proceed.
        """
        spec = parse_spec_sheet(sheet_text)
        verified, failures = spec.verify()
        self._save_spec(spec)
        if self.memory is not None:
            try:
                self.memory.record_episodic(
                    self.project_id,
                    f"Attached spec: {spec.goal}",
                    {"spec": spec.to_dict(), "deliverables": spec.deliverables,
                     "verified": verified, "attached": True})
            except Exception:
                pass
        approved = False
        if verified:
            if approve is not None:
                approved = bool(approve(spec))
            elif self.gate is not None:
                decision = self.gate.ask(
                    "spec", "Approve this spec sheet?",
                    spec_sheet_md(spec),
                    force_ask=True,
                )
                approved = decision.verdict in ("approved", "auto_approved")
        return InterviewResult(spec=spec, approved=approved,
                               verified=verified, failures=failures,
                               exchanges=[])

    @staticmethod
    def _prefill(spec: Spec, previous: Spec) -> Spec:
        """Fill empty spec fields from a previous spec, goal kept.

        Only empty fields are filled, never overwritten:
        a previous budget, constraint, or deliverable is a
        starting proposal, the current interview can change
        it, and the spec sheet card is the final check.
        """
        if not spec.deliverables:
            spec.deliverables = list(previous.deliverables)
        if not spec.constraints:
            spec.constraints = list(previous.constraints)
        if spec.budget_cents is None:
            spec.budget_cents = previous.budget_cents
        if not spec.deadline:
            spec.deadline = previous.deadline
        if not spec.recipients:
            spec.recipients = list(previous.recipients)
        if not spec.files:
            spec.files = list(previous.files)
        if not spec.notifications:
            spec.notifications = list(previous.notifications)
        if not spec.assumptions:
            spec.assumptions = list(previous.assumptions)
        return spec

    def _record_exchange(self, role: str, text: str, exchanges: list[dict]) -> None:
        exchanges.append({"role": role, "text": text})
        if self.memory is not None:
            try:
                self.memory.record_stm(
                    role, text, project_id=self.project_id,
                    conversation_id=self.project_id)
            except Exception:
                pass
