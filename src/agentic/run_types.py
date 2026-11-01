"""Planning data structures, risk helpers, and run-report formatting."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_ATTEMPTS = 3
MAX_PLAN_REPAIRS = 2

def _resolve_count(env_name: str, default: int, minimum: int) -> int:
    """Resolve a "how many times" knob: env var, else default, clamped.

    Invalid values fall back to the default, never to 0 for
    attempts (a run with zero attempts could never execute)
    or below the given minimum. Settings and .env.example
    carry the same names; the loop reads them per run, so a
    changed value applies to the next run.
    """
    try:
        value = int(os.environ.get(env_name, "") or default)
    except ValueError:
        return default
    return max(minimum, value)


def _resolve_critic_mode() -> str:
    """Critic scope: "risk" (default) or "always".

    Unknown values fall back to "risk": the default is the
    safe side for token spend, and a typo must never silently
    turn the critic into always-on (or off — off is
    AIDON_CRITIC_ENABLED=0, not a mode).
    """
    mode = (os.environ.get("AIDON_CRITIC_MODE", "") or "risk").strip().lower()
    return "always" if mode == "always" else "risk"


def _resolve_model_verifier_mode() -> str:
    """Model verifier scope: "off" (default), "selective", or "always".

    The default is the lowest: code verification decides alone and
    no model call is spent on a second opinion. Unknown values fall
    back to "off", so a typo must never silently turn the verifier
    into always-on.
    """
    mode = (os.environ.get("AIDON_MODEL_VERIFIER", "") or "off").strip().lower()
    return mode if mode in ("selective", "always") else "off"


def _model_verifier_wanted(mode: str, deliverables) -> bool:
    """True when the model verifier should run for these deliverables.

    "off" never runs; "always" always runs. "selective" runs unless
    every deliverable is structured data (JSON, JSONL, CSV, TSV),
    where code checks are complete -- existence, contents, and
    recomputed totals -- and a model opinion adds no quality
    signal. A prose, document, or image deliverable earns the
    second opinion.
    """
    if mode == "always":
        return True
    if mode != "selective":
        return False
    structured = {".json", ".jsonl", ".csv", ".tsv"}
    return any(
        Path(str(name)).suffix.lower() not in structured
        for name in (deliverables or []))


def _mentions_file(msg: str, name: str) -> bool:
    """True when a failure message names this exact file.

    A mention is an exact filename match, not a substring: the name
    must not be glued to another filename character on either side.
    A failure mentioning data.txt therefore does not mention a.txt,
    even though "a.txt" appears inside "data.txt" as text.
    """
    return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) +
                     r"(?![A-Za-z0-9_.-])", msg) is not None


def _critic_wanted(plan, adapted_from: str = "") -> bool:
    """True when the plan critic should run on this plan.

    In "always" mode it runs on every valid plan, the previous
    behavior. In "risk" mode (default) it runs only when the
    plan earns scrutiny: a fresh plan (not adapted from a past
    success) that is not small, or any plan with a live step —
    a tool whose name carries post/send/publish, the same
    signal the dispatcher uses for the live gate. Adapted
    plans already proved their shape, small plans have little
    to sense-check, and live steps always earn a second look.
    A plan whose steps cannot be read is treated as risky:
    the critic runs. AIDON_CRITIC_ENABLED=0 disables the
    critic entirely and is checked at the call site, not here.
    """
    if _resolve_critic_mode() == "always":
        return True
    try:
        steps = plan.normalized_steps()
    except Exception:
        return True
    live = any(
        any(word in (getattr(step, "tool", "") or "")
            for word in ("post", "send", "publish"))
        for step in steps)
    if live:
        return True
    small = len(steps) <= _resolve_count("AIDON_CRITIC_RISK_MAX_STEPS", 3, 0)
    return not (adapted_from or small)


@dataclass
class Step:
    tool: str = "write"
    args: dict = field(default_factory=dict)
    deliverable: str = ""
    criteria: str = ""
    description: str = ""
    executor: str = "tool"
    # DAG identity: steps from a DAG carry the node id and its
    # declared dependencies, so the executor can schedule
    # independent steps concurrently. Free-form model plans
    # leave these empty and always run sequentially: an
    # undeclared dependency must never be mistaken for
    # independence.
    id: str = ""
    depends_on: list[str] = field(default_factory=list)


# Tool-name fragments that mean the step leaves the workdir, touches a
# third party, spends money, deletes data, or installs code. A plan that
# contains any of them is not reversible, so a "live" pre-approval must
# not cover it. Conservative on purpose: a false "not reversible" only
# means one more card, a false "reversible" would skip one it should not.
SIDE_EFFECT_TOOL_WORDS = (
    "send", "post", "publish", "email", "mail", "install", "adopt",
    "delete", "spend", "pay", "upload", "record", "tts", "speak",
    "mcp_", "sam_", "transcribe", "threads", "instagram", "slack",
    "whatsapp", "notify", "schedule_job", "cancel_schedule",
)


def plan_is_reversible(plan: Plan, allowed_tools=None) -> bool:
    """True when every step stays inside the run's own workdir.

    Used by the pre-approval gate: a pre-approved "live" card is
    honoured only for a plan that cannot send, publish, spend,
    delete, or install anything. allowed_tools names steps a
    caller has already made safe — practice tools, whose
    destinations are forced to the owner and whose files are
    confined to the practice folder — so they do not trip
    the trigger-word check.
    """
    allowed = {str(t).lower() for t in (allowed_tools or ())}
    for step in plan.normalized_steps():
        name = str(getattr(step, "tool", "") or "").lower()
        if name in allowed:
            continue
        if any(word in name for word in SIDE_EFFECT_TOOL_WORDS):
            return False
    return True


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    summary: str = ""
    deliverable_descriptions: dict = field(default_factory=dict)
    needs_research: list[str] = field(default_factory=list)
    # True only for plans built from a DAG, where every
    # dependency is declared and undeclared means
    # independent. The executor runs such a plan's levels
    # concurrently; every other plan runs sequentially.
    parallel_ok: bool = False
    # Skill names the planner asked to have loaded in full
    # (level 2). Read by _plan for one expansion pass, then
    # ignored: the expanded plan's own list never triggers
    # a further pass.
    skills: list = field(default_factory=list)

    def normalized_steps(self) -> list[Step]:
        out = []
        for index, step in enumerate(self.steps):
            if isinstance(step, Step):
                if not step.id:
                    step.id = f"step-{index}"
                out.append(step)
            elif isinstance(step, dict):
                out.append(Step(
                    tool=str(step.get("tool", "write")),
                    args=dict(step.get("args", {})),
                    deliverable=str(step.get("deliverable") or ""),
                    criteria=str(step.get("criteria", "")),
                    description=str(step.get("description", "") or
                                    step.get("title", "")),
                    executor=str(step.get("executor", "tool")),
                    id=str(step.get("id", "") or f"step-{index}"),
                    depends_on=[str(d) for d in step.get("depends_on", [])]
                    if isinstance(step.get("depends_on", []), list) else []))
            else:
                out.append(Step(tool="write", args={"file": "deliverable.md",
                                                    "text": str(step)},
                                deliverable="deliverable.md",
                                id=f"step-{index}"))
        return out


@dataclass
class RunReport:
    status: str
    goal: str
    plan: Plan
    verified: bool
    approved: bool
    files: list[str] = field(default_factory=list)
    error: str = ""
    attempts: int = 1
    backend: str = "bubblewrap"
    model_calls: int = 0
    model_tokens_estimated: int = 0
    model_tokens_in_estimated: int = 0
    model_tokens_out_estimated: int = 0
    model_thinking_tokens: int = 0
    adapted: bool = False
    adapted_from: str = ""
    replan_note: str = ""
    learned_dag: str = ""
    chat_reply: str = ""
    # Text returned by successful tool steps that intentionally produce
    # no file. Kept in the transient report and surfaced to the owner;
    # it is not added to run-state or workflow-memory records.
    result_text: str = ""
    dag_summary: dict = field(default_factory=dict)
    step_lines: list[str] = field(default_factory=list)
    narration: str = ""
    delivery_summary: str = ""
    # Approval gates this run hit but never passed: rejected by a
    # human, or timed out while the owner was away. Rendered as
    # the "blocked actions" section of the final report, so a run
    # never silently dies at a gate.
    blocked: list[str] = field(default_factory=list)
    # Sandbox network state for this run: True means the
    # sandbox had network on (announced on stderr, shown on
    # approval cards), False means network off, None means
    # unknown (nothing announced, nothing shown).
    network: bool | None = None
    # Stopped Ask/Plan run: {"stop_id", "mode", "goal", ...} when
    # this report is a stop, else {}. The surface turns it into
    # the Proceed button (WebUI) or the proceed <id> reply text.
    mode_stop: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    approval_wait_seconds: float = 0.0
    # Token basis for the finished-run total: "actual" when every
    # model call reported provider usage (the in/out fields then
    # hold real counts), "estimated" otherwise (chars//4 split).
    # The in/out estimated fields always carry the
    # basis-appropriate values, so the footer breakdown sums to
    # the total; the actual fields hold the real counts alone.
    model_tokens_basis: str = "estimated"
    model_tokens_in_actual: int = 0
    model_tokens_out_actual: int = 0


def format_duration(seconds: float | int | None) -> str:
    """Render elapsed seconds in compact human units."""
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    if total >= 3600:
        hours, remainder = divmod(total, 3600)
        return f"{hours}h {remainder // 60}m"
    minutes, remainder = divmod(total, 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def format_elapsed(report: dict) -> str:
    """Render the compatible elapsed footer for a report dictionary."""
    elapsed = report.get("elapsed_seconds")
    if elapsed is None:
        return ""
    line = f"\n\n⏱️ {format_duration(elapsed)}"
    waited = float(report.get("approval_wait_seconds") or 0)
    if waited >= 1:
        line += f" (plus {format_duration(waited)} waiting for you)"
    return line


def format_run_stats(report: RunReport) -> str:
    """Format the completion metrics shown beside a finished task."""
    elapsed = max(0.0, float(getattr(report, "elapsed_seconds", 0.0) or 0.0))
    elapsed_text = format_duration(elapsed)
    tokens = int(getattr(report, "model_tokens_estimated", 0) or 0)
    in_tok = int(getattr(report, "model_tokens_in_estimated", 0) or 0)
    out_tok = int(getattr(report, "model_tokens_out_estimated", 0) or 0)
    think_tok = int(getattr(report, "model_thinking_tokens", 0) or 0)
    basis = str(getattr(report, "model_tokens_basis", "estimated")
                or "estimated")
    waited = float(getattr(report, "approval_wait_seconds", 0.0) or 0.0)
    waiting = (f" (plus {format_duration(waited)} waiting for you)"
               if waited >= 1 else "")
    if in_tok or out_tok or think_tok:
        usage = (f"IN: {in_tok:,} OUT: {out_tok:,} "
                 f"(Think: {think_tok:,}) TOT: {tokens:,} tokens ({basis})")
    else:
        # No breakdown recorded (a non-counting client): keep the
        # old single-number line rather than three zeros.
        usage = f"{tokens:,} total workflow tokens (estimated)"
    return f"⏱️ {elapsed_text}{waiting} · {usage}"


