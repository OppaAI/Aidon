"""Experience DB: procedural long-term memory, unified.

An Experience is a successful run's workflow, recorded so the
next run can reuse it. It is procedural memory, not a separate
store, and it lives in the Qdrant ``experience`` collection,
the same collection as other procedural records.

An Experience contains, in one record:
- goal: the goal text, and goal_shape: the shape of the goal
  (its key constraints, in plain words), so a future goal can
  be matched by shape, not just by exact text.
- dag_nodes: the workflow as a list of nodes, each node a dict
  with ``tool`` (or ``skill``), ``args``, and ``deliverable``.
  This is the DAG, in the same shape the loop executes.
- tools_used and skills_used: the tools and skills the run
  actually called, in order, so a future run knows what the
  workflow needs.
- outcome and lesson: what was delivered, and what the run
  learned that the next run should not repeat.

Build rule: Aidon builds an Experience after the FIRST
successful run, and after every successful run after that.
A failed or rejected run does not become an Experience,
because it did not prove a workflow that works. A failed
run becomes a FailedExample instead: a negative lesson in
the same collection, recalled at plan time so a similar
goal does not replay the failed approach. Rejected and
cancelled runs are not recorded — a human rejection or an
interruption is not evidence the approach was wrong.

Use rule: on a new goal, recall Experiences by goal
similarity first, before planning fresh. If a recalled
Experience matches the goal shape, its DAG nodes are the
starting plan, adapted to the new goal's constraints, and
then verified in the sandbox like any other plan. If no
Experience matches, plan fresh, and that fresh plan, if it
succeeds, becomes the next Experience.

This is the same idea the earlier design called Knowledge
(approved skills + DAGs). In this implementation it is called
Experience, and it is procedural memory, unified, not a
second knowledge store.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

from .embedder import tokens
from .memory import Memory, Point


def goal_shape_for(goal: str) -> str:
    """Deterministic goal shape: sorted unique key tokens.

    Shape matching is not exact-text matching. Two goals
    with the same constraints, in different words or order,
    share a shape, so a workflow for one can start the
    other. Stopwords are dropped, order is normalized.
    """
    stop = {"a", "an", "the", "for", "to", "and", "or", "of", "in", "on",
            "with", "i", "want", "please", "me", "my"}
    shape_tokens = sorted({t for t in tokens(goal) if t not in stop})
    return " ".join(shape_tokens)


def _shape_overlap(a: str, b: str) -> float:
    set_a, set_b = set(a.split()), set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


@dataclass
class Experience:
    goal: str
    goal_shape: str
    dag_nodes: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    outcome: str = ""
    lesson: str = ""
    generalized_nodes: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        """Text for embedding and recall: goal + shape + lesson."""
        return f"Goal: {self.goal}\nShape: {self.goal_shape}\nLesson: {self.lesson}"

    def to_payload(self) -> dict:
        return {
            "goal": self.goal,
            "goal_shape": self.goal_shape,
            "dag_nodes": json.dumps(self.dag_nodes),
            "tools_used": self.tools_used,
            "skills_used": self.skills_used,
            "outcome": self.outcome,
            "lesson": self.lesson,
            "generalized_nodes": json.dumps(self.generalized_nodes),
            "kind": "experience",
        }

    @classmethod
    def from_point(cls, point: Point) -> "Experience":
        payload = point.payload
        try:
            nodes = json.loads(payload.get("dag_nodes", "[]"))
        except (json.JSONDecodeError, TypeError):
            nodes = []
        try:
            gen = json.loads(payload.get("generalized_nodes", "[]"))
        except (json.JSONDecodeError, TypeError):
            gen = []
        return cls(
            goal=payload.get("goal", point.text),
            goal_shape=payload.get("goal_shape", ""),
            dag_nodes=nodes if isinstance(nodes, list) else [],
            tools_used=payload.get("tools_used", []),
            skills_used=payload.get("skills_used", []),
            outcome=payload.get("outcome", ""),
            lesson=payload.get("lesson", ""),
            generalized_nodes=gen if isinstance(gen, list) else [],
        )


def build_experience(goal: str, goal_shape: str, dag_nodes: list[dict],
                     tools_used: list[str], skills_used: list[str],
                     outcome: str, lesson: str = "", generalized_nodes: list[dict] | None = None) -> Experience:
    # An empty shape means "derive it from the goal", so
    # callers that pass the goal twice, as the loop does,
    # still get a real shape for matching.
    shape = goal_shape if goal_shape and goal_shape != goal else \
        goal_shape_for(goal)
    return Experience(goal=goal, goal_shape=shape,
                      dag_nodes=dag_nodes, tools_used=tools_used,
                      skills_used=skills_used, outcome=outcome,
                      lesson=lesson, generalized_nodes=generalized_nodes or [])


def store_experience(memory: Memory, experience: Experience) -> Point:
    """Store, merging on save when a close Experience already exists.

    Merge rule: if an existing Experience shares at least
    half its shape tokens (Jaccard >= 0.5) and at least one
    tool, the new DAG nodes are unioned into the existing
    point (dedupe by deliverable), tools and skills are
    unioned, and the lesson is appended, instead of creating
    a duplicate. The library of workflows heals itself
    instead of duplicating.

    Two compatibility gates keep a merge from producing an
    unreliable workflow. First, a deliverable (or node id)
    produced with different arguments is a different
    procedure with the same name, not the same workflow
    twice: merging it would silently keep the old steps
    and drop the new ones, so the two experiences stay
    separate instead. Second, the merged node set must
    pass DAG validation (no duplicate ids or deliverables,
    no missing or cyclic dependencies, no unknown
    executors) before it replaces the stored point; a
    merged set that fails validation is not saved, and
    the new experience is stored separately.
    """
    if not experience.goal_shape:
        experience.goal_shape = goal_shape_for(experience.goal)
    candidates = recall_experiences(memory, experience.goal, limit=5)
    for candidate in candidates:
        if _shape_overlap(candidate.goal_shape, experience.goal_shape) < 0.5:
            continue
        if not (set(candidate.tools_used) & set(experience.tools_used)):
            continue
        # Compatibility gate 1: same name, different procedure.
        by_deliverable = {n.get("deliverable"): n
                          for n in candidate.dag_nodes}
        by_id = {n.get("id"): n for n in candidate.dag_nodes
                 if n.get("id")}
        compatible = True
        for node in experience.dag_nodes:
            old = by_deliverable.get(node.get("deliverable"))
            if (old is not None and node.get("deliverable")
                    and old.get("args") != node.get("args")):
                compatible = False
                break
            old_id = by_id.get(node.get("id")) if node.get("id") else None
            if old_id is not None and old_id != node:
                compatible = False
                break
        if not compatible:
            continue
        # Merge into the existing point, by re-recording it.
        merged_nodes = list(candidate.dag_nodes)
        seen = {n.get("deliverable") for n in merged_nodes}
        for node in experience.dag_nodes:
            if node.get("deliverable") not in seen:
                merged_nodes.append(node)
                seen.add(node.get("deliverable"))
        # Compatibility gate 2: the merged set must validate
        # as the DAG the adapt path will actually execute.
        try:
            from ..agentic.dag import dag_from_experience as _dfe
            _dfe(Experience(goal=candidate.goal,
                            goal_shape=candidate.goal_shape,
                            dag_nodes=merged_nodes)).validate_strict()
        except Exception:
            continue
        merged = Experience(
            goal=candidate.goal, goal_shape=candidate.goal_shape,
            dag_nodes=merged_nodes,
            tools_used=sorted(set(candidate.tools_used) | set(experience.tools_used)),
            skills_used=sorted(set(candidate.skills_used) | set(experience.skills_used)),
            generalized_nodes=experience.generalized_nodes or candidate.generalized_nodes,
            outcome=experience.outcome or candidate.outcome,
            lesson=(candidate.lesson + "\n" + experience.lesson).strip()
            if experience.lesson else candidate.lesson)
        # Delete the old point by matching its text, then
        # record the merged one. Points are found by recall,
        # so we locate the exact point id first.
        for point in memory.iter_points("experience"):
            if point.payload.get("goal") == candidate.goal and \
                    point.payload.get("goal_shape") == candidate.goal_shape:
                memory.forget("experience", point.id)
                break
        return memory.record("experience", merged.to_text(), merged.to_payload())
    return memory.record("experience", experience.to_text(),
                         experience.to_payload())


def recall_experiences(memory: Memory, goal: str,
                       limit: int = 3) -> list[Experience]:
    hits = memory.recall("experience", goal, limit=limit, graph=False)
    shape = goal_shape_for(goal)
    scored = []
    for hit in hits:
        # FailedExample points share this collection, but they
        # are negative lessons, not reusable workflows: they
        # must never occupy a success-recall slot or reach
        # the adapt path, which expects proven dag_nodes.
        if (hit.point.payload or {}).get("kind") == "failed_example":
            continue
        exp = Experience.from_point(hit.point)
        # Re-rank: vector score plus shape overlap, so a
        # workflow with the same constraints beats a workflow
        # with similar words but a different shape.
        scored.append((hit.score + _shape_overlap(exp.goal_shape, shape), exp))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [exp for _, exp in scored[:limit]]


def experience_skill_name(experience: Experience) -> str:
    """Filename-safe skill name for an experience: slugged goal."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", experience.goal.lower()).strip("-")
    return slug or "skill"


def render_experience_skill(experience: Experience) -> str:
    """Render a successful run's workflow as a planner-readable skill.

    The planner discovers standalone skills from <home>/skills/
    Markdown files, so this is the format a promoted workflow
    takes: the goal it solves, the steps that solved it (tool,
    deliverable, and arguments), and the lesson the run left
    behind. Provenance records that the workflow comes from
    one successful run, not from a vetted library skill, so
    a reader can tell what kind of evidence backs it.
    """
    lines = [
        f"# Skill: {experience_skill_name(experience)}",
        "",
        f"Learned from a successful run of: {experience.goal}",
        f"Outcome: {experience.outcome or 'done'}",
        "",
        "## Workflow",
        "",
    ]
    for index, node in enumerate(experience.dag_nodes, 1):
        tool = node.get("tool", "write")
        deliverable = node.get("deliverable", "")
        args = node.get("args", {})
        detail = f" -> {deliverable}" if deliverable else ""
        lines.append(f"{index}. {tool}{detail}")
        if args:
            lines.append(f"   args: {args}")
    if not experience.dag_nodes:
        lines.append("(no steps recorded)")
    lines += ["", "## Lesson", "", experience.lesson or "(none)", "",
              "## Provenance", "",
              f"- source: successful run (experience, shape: {experience.goal_shape})",
              f"- tools: {', '.join(experience.tools_used) or '(none)'}",
              ""]
    return "\n".join(lines)


def promote_experience_to_skill(home, experience: Experience,
                                name: str | None = None):
    """Write an experience out as a discoverable skill file.

    The file lands in <home>/skills/<name>.md, where the
    planner's skill discovery already reads it, so the next
    run can reuse the workflow. An existing file is never
    overwritten silently: the owner edits or deletes a
    hand-tuned skill by hand, and a promotion must not
    clobber it. Promotion is an explicit owner action (the
    skill-library CLI), not something a run does to itself.
    """
    from pathlib import Path as _Path
    skills_dir = _Path(home) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    filename = (name or experience_skill_name(experience)) + ".md"
    path = skills_dir / filename
    if path.exists():
        raise FileExistsError(
            f"skill file already exists: {path} (delete or rename it first)")
    path.write_text(render_experience_skill(experience), encoding="utf-8")
    return path


# --- Failed examples: negative lessons, same collection ---

# One knob for "same goal shape", shared by DAG learning,
# experience adaptation, DAG adaptation, composition recall,
# and failure matching: AIDON_DAG_SIMILARITY, default 0.3.
# Read at call time, never at import, so a settings change
# applies to the next run. Garbage falls back to the
# default; out-of-range values clamp into [0, 1], so 1.0
# means "identical shape only" and 0.0 means "everything
# matches".
DEFAULT_DAG_SIMILARITY = 0.3


def dag_similarity_threshold() -> float:
    raw = os.environ.get("AIDON_DAG_SIMILARITY", "")
    if not str(raw).strip():
        return DEFAULT_DAG_SIMILARITY
    try:
        value = float(str(raw).strip())
    except ValueError:
        return DEFAULT_DAG_SIMILARITY
    if not math.isfinite(value):
        return DEFAULT_DAG_SIMILARITY
    return min(1.0, max(0.0, value))


@dataclass
class FailedExample:
    goal: str
    goal_shape: str
    error: str = ""
    failed_tools: list[str] = field(default_factory=list)
    failed_deliverables: list[str] = field(default_factory=list)
    lesson: str = ""

    def to_text(self) -> str:
        return (f"Failed goal: {self.goal}\nShape: {self.goal_shape}\n"
                f"Error: {self.error}\nLesson: {self.lesson}")

    def to_payload(self) -> dict:
        return {
            "goal": self.goal,
            "goal_shape": self.goal_shape,
            "dag_nodes": "[]",
            "tools_used": list(self.failed_tools),
            "skills_used": [],
            "outcome": f"failed: {self.error}",
            "lesson": self.lesson,
            "failed_deliverables": list(self.failed_deliverables),
            "kind": "failed_example",
        }


def record_failure(memory: Memory, goal: str, error: str = "",
                   failed_tools: list[str] | None = None,
                   failed_deliverables: list[str] | None = None) -> Point:
    """Record a failed run as a negative lesson, no model call.

    The lesson is deterministic text built from the error,
    the tools used, and the deliverables never produced, so
    a future planner can see exactly what not to repeat.
    """
    tools = [t for t in dict.fromkeys(failed_tools or [])]
    deliverables = [d for d in dict.fromkeys(failed_deliverables or [])]
    err = (error or "")[:500]
    lesson = (f"Failed: {err or 'unknown error'}. "
              f"Failed tools: {', '.join(tools) or 'none'}. "
              f"Failed deliverables: {', '.join(deliverables) or 'none'}. "
              f"Do not repeat this approach for a similar goal.")
    example = FailedExample(
        goal=goal, goal_shape=goal_shape_for(goal), error=err,
        failed_tools=tools, failed_deliverables=deliverables, lesson=lesson)
    return memory.record("experience", example.to_text(), example.to_payload())


def recall_failures(memory: Memory, goal: str,
                    limit: int = 3) -> list[FailedExample]:
    """Recall negative lessons for a goal, best shape match first."""
    hits = memory.recall("experience", goal, limit=limit, graph=False)
    shape = goal_shape_for(goal)
    scored = []
    for hit in hits:
        payload = hit.point.payload or {}
        if payload.get("kind") != "failed_example":
            continue
        example = FailedExample(
            goal=payload.get("goal", hit.point.text),
            goal_shape=payload.get("goal_shape", ""),
            error=payload.get("outcome", "").removeprefix("failed: "),
            failed_tools=payload.get("tools_used", []),
            failed_deliverables=payload.get("failed_deliverables", []),
            lesson=payload.get("lesson", ""))
        scored.append((_shape_overlap(example.goal_shape, shape), example))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [example for _, example in scored[:limit]]


def matching_failure(memory: Memory, goal: str) -> FailedExample | None:
    """Best negative lesson for a goal, or None when unrelated.

    Same shape threshold as the adapt path (AIDON_DAG_SIMILARITY,
    default 0.3): a failure about a different task must not
    steer this goal's plan.
    """
    for example in recall_failures(memory, goal, limit=3):
        if _shape_overlap(example.goal_shape, goal_shape_for(goal)) >= dag_similarity_threshold():
            return example
    return None
