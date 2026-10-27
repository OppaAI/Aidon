"""Planner, old Aidon planner concept, rebuilt, deterministic, no model call.

plan_goal builds a plan from a goal, a pattern, and available tools: if a pattern matches,
its steps are used, otherwise a single write step is used, with the goal as deliverable
context. The plan is validated by the DAG validator before it is returned, so a planner
output that fails validation is not handed to the loop as if it were valid.
"""

from __future__ import annotations

from .dag import DAG, DAGNode
from .patterns import match_pattern


def plan_goal(goal: str, tools: list | None = None) -> dict:
    pattern = match_pattern(goal)
    if pattern:
        steps = [{"tool": step.get("tool", "write"), "args": {"goal": goal}, "deliverable": step.get("deliverable", "output"), "assumptions": []} for step in pattern["steps"]]
    else:
        steps = [{"tool": "write", "args": {"goal": goal}, "deliverable": "output", "assumptions": []}]
    nodes = []
    for index, step in enumerate(steps):
        node = {"id": f"step_{index}", "tool": step["tool"], "args": step["args"], "deliverable": step["deliverable"], "assumptions": step["assumptions"], "depends_on": [f"step_{index-1}"] if index else []}
        nodes.append(node)
    dag = DAG([DAGNode.from_dict(node) for node in nodes])
    failures = dag.validate()
    return {"goal": goal, "pattern": pattern["name"] if pattern else None, "nodes": nodes, "valid": not failures, "failures": failures}


# -- model planner with defensive JSON parsing (added, plan_goal kept) ------

import json
import re
from dataclasses import dataclass, field


class UnknownToolError(ValueError):
    """The planner named a tool that is not in the registry."""

    def __init__(self, tool: str, step_id: str = ""):
        super().__init__(f"planner: step {step_id} uses unknown tool {tool!r}")
        self.tool = tool
        self.step_id = step_id


@dataclass
class PlanStep:
    id: str
    title: str
    tool: str
    args: dict = field(default_factory=dict)
    deps: list = field(default_factory=list)
    executor: str = "tool"

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "tool": self.tool,
                "args": self.args, "deps": list(self.deps),
                "executor": self.executor}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        return cls(id=data["id"], title=data.get("title", data["id"]),
                   tool=data["tool"], args=dict(data.get("args", {})),
                   deps=list(data.get("deps", [])),
                   executor=data.get("executor", "tool"))


@dataclass
class PlanSpec:
    title: str
    goal: str
    steps: list = field(default_factory=list)
    acceptance_criteria: list = field(default_factory=list)
    skills_needed: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    needs_research: list = field(default_factory=list)
    vars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"title": self.title, "goal": self.goal,
                "steps": [s.to_dict() for s in self.steps],
                "acceptance_criteria": list(self.acceptance_criteria),
                "skills_needed": list(self.skills_needed),
                "risks": list(self.risks),
                "needs_research": list(self.needs_research),
                "vars": dict(self.vars)}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanSpec":
        return cls(
            title=data.get("title", ""), goal=data.get("goal", ""),
            steps=[PlanStep.from_dict(s) for s in data.get("steps", [])],
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            skills_needed=list(data.get("skills_needed", [])),
            risks=list(data.get("risks", [])),
            needs_research=list(data.get("needs_research", [])),
            vars=dict(data.get("vars", {}))
            if isinstance(data.get("vars"), dict) else {})

    def to_dag(self, name: str = "") -> DAG:
        # The new DAGNode has no title field; the step id doubles as
        # its unique deliverable so validation stays meaningful.
        return DAG([DAGNode(id=s.id, tool=s.tool, args=dict(s.args),
                            deliverable=s.id,
                            depends_on=list(s.deps)) for s in self.steps])

    def render(self) -> str:
        lines = [f"# Plan: {self.title}", "", f"Goal: {self.goal}", "",
                 "## Steps"]
        for step in self.steps:
            deps = f" (after {', '.join(step.deps)})" if step.deps else ""
            lines.append(f"{step.id}. **{step.title}** — `{step.tool}`{deps}")
        lines += ["", "## Acceptance criteria"]
        lines += [f"- {c}" for c in self.acceptance_criteria] or ["- (none)"]
        if self.skills_needed:
            lines += ["", "## Skills needed",
                      *[f"- {s}" for s in self.skills_needed]]
        if self.risks:
            lines += ["", "## Risks", *[f"- {r}" for r in self.risks]]
        return "\n".join(lines)

    def render_candidates(self) -> str:
        lines = []
        for index, candidate in enumerate(self.candidates or [], 1):
            if isinstance(candidate, dict):
                lines.append(f"{index}. {candidate.get('approach', '?')} — "
                             f"{candidate.get('why', '')}".rstrip(" —"))
            else:
                lines.append(f"{index}. {candidate}")
        return "\n".join(lines) or "(no alternatives listed)"


def _repair_escapes(text: str) -> str:
    """Double stray backslashes inside string literals.

    Models writing shell commands and regexes into plan JSON emit
    things like \\$ or \\d — none of them valid JSON escapes, so
    json.loads rejects the whole plan. A backslash followed by a
    valid escape character is copied untouched, so a valid document
    passes through unchanged; only the invalid ones are doubled,
    which preserves the character the model meant."""
    valid = set('"\\/bfnrtu')
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        char = text[i]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue
        if char == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in valid:
                out.append(text[i:i + 2])
                i += 2
            else:
                out.append("\\\\")
                i += 1
            continue
        out.append(char)
        if char == '"':
            in_string = False
        i += 1
    return "".join(out)


def _repair_json(raw: str) -> str:
    """Best-effort fix for the JSON dialects models actually emit:
    code fences, stray backslash escapes, trailing commas, and
    single-quoted keys. Anything else still raises in json.loads."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = _repair_escapes(text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"(?<=[{,]\s)'([^'\"]+)'(\s*:)", r'"\1"\2', text)
    return text


def _extract_json(text: str) -> dict:
    """Pull the first {...} JSON object out of model output, fences
    or not, repairing it once if plain parsing fails."""
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in model output")
    raw = stripped[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        if repaired != raw:
            return json.loads(repaired)
        raise


def _response_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return str(resp.get("text", ""))
    return str(getattr(resp, "text", resp))


def make_plan(client, goal: str, research_notes: str = "",
              past_experience: str = "", tool_names=None,
              on_response=None, guidance: str = "",
              tool_schemas: dict | None = None, chat_fn=None) -> PlanSpec:
    """Ask the model for a plan; parse it defensively and validate
    it into a PlanSpec. One corrective retry on unparseable output:
    the model is told exactly what broke and asked for JSON only."""
    from .prompts import guidance_block
    tool_names = list(tool_names or [])
    schema_block = ""
    if tool_schemas:
        lines = []
        for name in tool_names:
            schema = tool_schemas.get(name) or {}
            fn = schema.get("function", schema)
            params = fn.get("parameters") or {}
            props = params.get("properties", {})
            required = params.get("required", [])
            arglist = ", ".join(
                f"{k}{'*' if k in required else ''}" for k in props) \
                or "(no args)"
            lines.append(f"- {name}({arglist})")
        schema_block = ("\nAvailable tools (name(args), * = required):\n"
                        + "\n".join(lines) + "\n")
    prompt = (
        f"Goal: {goal}\n\nResearch notes:\n{research_notes or '(none)'}\n\n"
        f"Past experience:\n{past_experience or '(none)'}\n\n"
        f"{guidance_block(guidance)}{schema_block}"
        "Reply with ONLY a JSON object: {\"title\": ..., \"steps\": "
        "[{\"id\": ..., \"title\": ..., \"tool\": ..., \"args\": {...},"
        " \"deps\": [...]}], \"acceptance_criteria\": [...]}")
    chat = chat_fn if chat_fn is not None else client.chat
    resp = chat([{"role": "user", "content": prompt}])
    if on_response is not None:
        try:
            on_response(resp, 0)
        except Exception:
            pass
    try:
        data = _extract_json(_response_text(resp))
    except (ValueError, json.JSONDecodeError) as exc:
        retry_prompt = (
            f"{prompt}\n\nIMPORTANT: your previous reply was not valid JSON "
            f"({exc}). Reply with ONLY the JSON object, no prose, no code "
            "fence, no trailing commas, and double-quote every key.")
        resp2 = chat([{"role": "user", "content": retry_prompt}])
        if on_response is not None:
            try:
                on_response(resp2, 0)
            except Exception:
                pass
        try:
            data = _extract_json(_response_text(resp2))
        except (ValueError, json.JSONDecodeError) as exc2:
            raise ValueError(f"planner: could not parse plan JSON: {exc2}\n"
                             f"Raw:\n{_response_text(resp2)[:2000]}")
    steps = []
    for index, raw_step in enumerate(data.get("steps", [])):
        tool = raw_step.get("tool", "")
        if tool not in tool_names and tool != "llm" \
                and not tool.startswith("mcp_"):
            raise UnknownToolError(tool, raw_step.get("id", index))
        if tool == "llm" and not (raw_step.get("args") or {}).get("prompt"):
            raise ValueError(
                f"planner: step {raw_step.get('id', index)} llm node needs"
                " args.prompt")
        if tool_schemas:
            schema = tool_schemas.get(tool) or {}
            fn = schema.get("function", schema)
            required = (fn.get("parameters") or {}).get("required", [])
            args = raw_step.get("args", {}) or {}
            missing = [k for k in required if k not in args]
            if missing:
                raise ValueError(
                    f"planner: step {raw_step.get('id', index)} ({tool})"
                    f" missing required args: {', '.join(missing)}")
        executor = raw_step.get("executor", "tool")
        if executor not in ("tool", "fast", "strong"):
            executor = "tool"
        if tool == "llm" and executor == "tool":
            executor = "strong"
        steps.append(PlanStep(
            id=raw_step.get("id", f"step{index + 1}"),
            title=raw_step.get("title", f"step {index + 1}"),
            tool=tool, args=raw_step.get("args", {}),
            deps=raw_step.get("deps", []), executor=executor))
    plan = PlanSpec(
        title=data.get("title", "Untitled plan"), goal=goal, steps=steps,
        acceptance_criteria=data.get("acceptance_criteria", []),
        skills_needed=data.get("skills_needed", []),
        risks=data.get("risks", []),
        candidates=data.get("candidate_approaches", []) or [],
        needs_research=[str(x) for x in data.get("needs_research", []) or []],
        vars={str(k): v for k, v in (data.get("vars") or {}).items()}
        if isinstance(data.get("vars"), dict) else {})
    failures = plan.to_dag().validate()
    if failures:
        raise ValueError(f"planner: plan graph invalid: {'; '.join(failures)}")
    return plan


def verify_acceptance(client, goal: str, criteria: list[str], evidence: str,
                      on_response=None, chat_fn=None) -> dict:
    """Ask the model to judge evidence against acceptance criteria.
    An empty criteria list falls back to the goal itself, so an
    empty list can never pass vacuously. Unparseable verifier
    output is a failure, not a pass."""
    if not criteria:
        criteria = [f"the deliverable satisfies the goal: {goal[:300]}"]
    prompt = ("Goal: " + goal + "\n\nCriteria:\n"
              + "\n".join(f"- {c}" for c in criteria)
              + "\n\nEvidence:\n" + evidence[:12000]
              + "\n\nReply with ONLY a JSON object: {\"passed\": true/false,"
                " \"results\": [{\"criterion\": ..., \"passed\": ...,"
                " \"evidence\": ...}], \"notes\": ...}")
    chat = chat_fn if chat_fn is not None else client.chat
    resp = chat([{"role": "user", "content": prompt}])
    if on_response is not None:
        try:
            on_response(resp, 0)
        except Exception:
            pass
    try:
        return _extract_json(_response_text(resp))
    except (ValueError, json.JSONDecodeError):
        return {"passed": False,
                "notes": "verifier output unparseable — treating as not passed",
                "missing": list(criteria)}
