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
history) and as episodic memory, scoped by conversation_id,
so a later run can see what was asked and answered,
and a correction updates the spec, it does not start
a new interview. The spec itself is saved as JSON
under <home>/specs/<conversation_id>.json, and recorded
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
    criteria: list[str] = field(default_factory=list)
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
            "criteria": self.criteria,
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
            criteria=as_list(data.get("criteria", [])),
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
        for item in self.criteria:
            if item.strip().lower() in PLACEHOLDERS:
                failures.append(f"done-when criterion is a placeholder: {item}")
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
    sheet.extend(lines("Done when", spec.criteria))
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
        "You can also edit a frozen spec on the sheet itself in chat, save it, and proceed. "
        "Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo. "
        "Docs: README, Spec sheet section.",
        "",
        f"## Goal",
        "",
        spec.goal or "None",
        "",
        "## Done when",
        "",
        bullets(spec.criteria),
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

How to use this sheet: fill in the three required sections (Goal, Done when, Constraints). Everything else is optional. Blank means "use Aidon's configured defaults", and blank never triggers a question.
Save it and attach it in the chat box. Aidon reads it, verifies it, and skips the interview. You can also edit a frozen spec on the sheet itself in chat, save it, and proceed.

Prefill: Aidon may prefill non-critical fields from your past tasks and memory. Prefilled values are proposals, not facts: review them, edit them, or delete them. Aidon never prefills Budget, Recipients, or anything that sends, spends, or publishes.

Permissions rule: unspecified permissions follow Aidon's enforced policy; a blank field never grants additional authority. Actions that need approval stay gated even after the plan is approved. To restrict further, write it as a Constraint.

Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Example: examples/SPEC-SHEET-EXAMPLE.md in the repo.
Docs: README, Spec sheet section.

## Goal

Write the goal in plain language, in one sentence, saying what you want Aidon to do and for whom, not how it will do it. This section is required.

## Done when

Write checkable acceptance criteria, one per bullet. Aidon verifies each one before reporting done. Prefer things it can test (a command, a count, a total, a source URL) over opinions. This section is required.

## Constraints

List every hard limit Aidon must not break, one per bullet, such as a date range, a format, or a source file. Write None if there are no hard limits. This section is required.
Write scope, priorities, and permission restrictions here too, one per bullet, for example "Out of scope: ...", "Priority: accuracy over speed", or "Never: ...".

## Deliverables

List each file or concrete outcome Aidon must produce, one per bullet, with the file name if it is a file, and the properties it must have.

## Inputs and context

List every input file, link, repo, or dataset Aidon should read first, one per bullet, with what each is for. Say who the audience is and what tone to use if it matters.

## Budget

Write the maximum total amount Aidon may spend, in dollars and cents, or write None if there is no budget cap. Do not write a budget here unless it is a real limit. Write other limits (a time cap, a max step count) as Constraints.

## Deadline

Write the date and time the work must be finished by, or write No deadline if there is none.

## Recipients

List who the work is for or who should receive the output, with their contact details if Aidon must send it to them, or write None if there is no recipient.

## Files

List every output file Aidon should create, one per bullet, with the file name and path. Input files go under Inputs and context.

## Notifications

Write how you want to be told when the work is done or if it fails, or write None if no notification is needed. A checkpoint (pause for review after a phase) goes here as "Checkpoint: ...".

## Permissions

Aidon enforces its own policy; this table is the default, not a grant. Override only by writing a Constraint.

| Action | Level |
|---|---|
| Read files, browse, research | auto |
| Write or edit files in the workspace | auto |
| Run code, scripts, tests in the sandbox | auto |
| Install packages or tools | ask |
| Create and register new tools, skills, or MCP servers | ask |
| Send email or messages to other people | ask (draft first) |
| Spend real money or make purchases | never |
| Post or publish anything publicly | never |

## If something goes wrong

Aidon preserves successful outputs, diagnoses the failure, attempts a bounded repair, re-runs the affected checks and any necessary regression checks, records the failure and repair outcome, and stops and reports if the retry limit is reached or safe progress is no longer possible. To change this, write a bullet under Assumptions starting with "Recovery:".

## Assumptions

List anything Aidon may assume without asking, one per bullet, or write None if there are no assumptions. Aidon adds its own assumptions to the plan for you to confirm.

## Open questions

List any questions that are still open, one per bullet, or write None if the spec is complete. A spec with open questions is not verified, and Aidon asks only about the ones that block the work.

## What Aidon does with this sheet

1. Validate: checks that the required sections are filled and the criteria are testable.
2. Fill gaps: infers defaults and states each assumption instead of interviewing. It asks only when a missing answer would change the work or cause something irreversible.
3. Plan: writes a short plan covering steps, tools, assumptions, and how each "Done when" item will be checked.
4. Execute: works within permissions and limits, and keeps a run log of actions, spend, and decisions.
5. Verify: checks every "Done when" item and reports pass or fail with evidence.
6. Report: delivers the outputs and a short summary with the evidence and outcome for each acceptance check, estimates, unverified claims, limitations, and failed checks, the files created or modified and any new capabilities, and any outstanding approvals or actions not performed. Final status: Success, Partial success, or Failed.
"""


def _parse_budget(text: str) -> int | None:
    """Money only: cents, a $ amount, or a bare number alone.

    A line that merely contains a number ("Time cap: 60 min",
    "3 files") is not a budget and must not become one.
    """
    import re
    text = text.strip()
    if not text or text == "-":
        return None
    match = re.search(r"(\d+)\s*cents", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+(?:\.\d{1,2})?)\s*dollars", text, re.IGNORECASE)
    if match:
        return int(round(float(match.group(1)) * 100))
    match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if match:
        return int(round(float(match.group(1)) * 100))
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return int(round(float(text) * 100))
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
    section_prefix = ""
    sections = {
        "goal": "goal", "done when": "criteria",
        "acceptance criteria": "criteria",
        "deliverables": "deliverables",
        "constraints": "constraints", "budget": "budget",
        "deadline": "deadline", "recipients": "recipients",
        "files": "files", "inputs": "files", "inputs and context": "files",
        "notifications": "notifications",
        "assumptions": "assumptions", "open questions": "open_questions",
        "priorities": ("constraints", "Priority: "),
        "out of scope": ("constraints", "Out of scope: "),
        "permissions": ("constraints", "Permission: "),
        "if something goes wrong": ("assumptions", "Recovery: "),
        "recovery": ("assumptions", "Recovery: "),
        "checkpoints and reporting": ("notifications", "Checkpoint: "),
        "checkpoints": ("notifications", "Checkpoint: "),
    }

    def _resolve(heading: str) -> tuple[str, str]:
        value = sections[heading]
        if isinstance(value, tuple):
            return value
        return value, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in ("none", "no deadline", "-"):
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if heading in sections and heading != "spec sheet":
                section, section_prefix = _resolve(heading)
            continue
        if ":" in line and not line.startswith(("-", "*")):
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in sections:
                section, section_prefix = _resolve(key)
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
                    getattr(spec, section).append(section_prefix + value)
                continue
        if line.startswith(("-", "*")):
            item = line.lstrip("-*").strip()
            if not item or item == "-" or item.lower() == "none":
                continue
            if section == "goal":
                spec.goal = item
            elif section in ("deliverables", "criteria", "constraints",
                             "recipients", "files", "notifications",
                             "assumptions", "open_questions"):
                getattr(spec, section).append(section_prefix + item)
            continue
        # A bare line under Goal, Budget, or Deadline is that field.
        if section == "goal" and not spec.goal and line != "-":
            spec.goal = line
        elif section == "budget" and spec.budget_cents is None:
            spec.budget_cents = _parse_budget(line)
        elif section == "deadline" and not spec.deadline and line != "-":
            spec.deadline = line
    return spec


# Words that carry no task meaning: greetings, question words,
# pronouns, and generic verbs. The prefill gate below keeps only
# the rest, so "hey, how are you" shares nothing with any task.
_PREFILL_STOPWORDS = frozenset(
    "a an the and or to of for in on with at by me my i you he she it "
    "we they is are was were be been being please hey hi hello how what "
    "why when where who which can could would should do does did make "
    "get give help want need just really".split())


def _content_words(text: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _PREFILL_STOPWORDS


def _goals_related(new_goal: str, prev_goal: str) -> bool:
    """Prefill seeds a continuation, never a new task.

    A greeting shares no content word with the previous task's
    goal, so it can never inherit that task's spec. Without this
    gate, a greeting after a failed task was interviewed about
    that task's gaps: the prefill took the oldest spec in the
    chat, whatever the user just said.
    """
    return bool(_content_words(new_goal) & _content_words(prev_goal))


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
                 gate=None, conversation_id: str = "",
                 max_questions: int = 20):
        self.client = client
        self.home = Path(home)
        self.memory = memory
        self.gate = gate
        self.conversation_id = conversation_id
        self.max_questions = max_questions

    def _reply(self, messages) -> str:
        """One model answer for the interview, streamed when possible.

        A non-streaming request sends nothing back until the whole
        answer is ready, so a slow thinking model can sit past the
        socket read timeout and fail the whole interview with
        "The read operation timed out". Streaming hands back chunks as
        they arrive, which keeps the connection alive and turns the
        wait into visible progress.

        The base client's ``chat_stream`` yields one block, so this
        degrades to exactly the old call for clients that do not
        stream. A stream that dies before its first chunk retries once
        non-streaming; one that dies mid-answer does not, because a
        retry there could duplicate text.
        """
        stream = getattr(self.client, "chat_stream", None)
        if not callable(stream):
            return self.client.chat(messages)
        chunks: list[str] = []
        try:
            for delta in stream(messages):
                if delta:
                    chunks.append(str(delta))
        except Exception:
            if chunks:
                raise
            return self.client.chat(messages)
        text = "".join(chunks)
        return text if text.strip() else self.client.chat(messages)

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
        # - previous conversation in this chat,
        # - previous episodic specs for a similar goal,
        # - previous successful Experiences for a similar goal.
        # A prefilled field is a proposal, not a fact: the
        # user can correct it in chat, and the spec sheet
        # card at the end is where they approve or reject it.
        memory_context: dict = {}
        initial_spec = Spec(goal=message.strip())
        if self.memory is not None:
            try:
                context = self.memory.conversation_context(self.conversation_id)
                memory_context["previous_exchanges"] = [
                    {"role": p.payload.get("role", ""), "text": p.text}
                    for p in context["exchanges"][-5:]]
                if context["summary"]:
                    memory_context["previous_summary"] = context["summary"]
                # Previous specs: the NEWEST episodic spec from
                # THIS chat only, and only when the new message
                # is about the same task (shares a content word
                # with that spec's goal). List order is oldest
                # first, so taking the first match seeded a new
                # interview with the oldest task's spec — a
                # greeting after a failed task got interviewed
                # about that task. A spec from another chat must
                # never seed this interview, and a proceeded or
                # rejected spec needs no special case: an
                # unrelated message shares nothing with it, so
                # the gate already keeps it from seeding again.
                best = None
                best_created = ""
                for point in self.memory.list_points("episodic", limit=100):
                    if point.payload.get("conversation_id") != self.conversation_id:
                        continue
                    saved = point.payload.get("spec")
                    if not (isinstance(saved, dict) and saved.get("goal")):
                        continue
                    created = str(point.payload.get("created_at", ""))
                    if best is None or created >= best_created:
                        best, best_created = saved, created
                if best is not None and _goals_related(
                        message, str(best.get("goal", ""))):
                    previous = Spec.from_dict(best)
                    spec = self._prefill(spec, previous)
                    memory_context["previous_spec"] = previous.to_dict()
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
            reply = self._reply([
                Message("system",
                        "You are interviewing the user to build a frozen "
                        "spec. Reply as JSON with keys question and spec. "
                        "If the spec is complete, question is null and "
                        "complete is true. Fill non-critical fields from "
                        "the spec and from_memory context instead of asking "
                        "about them: prefilled values are proposals the user "
                        "reviews on the sheet. Ask one question at a time, "
                        "in chat, and only for a field that is missing AND "
                        "would change the work or is irreversible. If the goal involves encryption and no algorithm is specified, ask which encryption algorithm to use, offering AES-256-GCM as the default."),
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
                    self.conversation_id,
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

        Both live in <home>/specs/: <conversation>.json for code,
        <conversation>.md for humans. The template is ensured
        alongside them, so a user who finds a sheet knows
        they can write their own. Even an unverified spec
        is saved, so the user can see what is missing and
        resume it.
        """
        spec_dir = self.home / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_template()
        json_path = spec_dir / f"{self.conversation_id}.json"
        md_path = spec_dir / f"{self.conversation_id}.md"
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
                    self.conversation_id,
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
                    role, text, conversation_id=self.conversation_id)
            except Exception:
                pass
