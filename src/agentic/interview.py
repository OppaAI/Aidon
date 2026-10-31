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

from .spec import (PLACEHOLDERS, SPEC_SHEET_TEMPLATE, Spec, _parse_budget, parse_spec_sheet,
                   spec_sheet, spec_sheet_md)


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
