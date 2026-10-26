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
because it did not prove a workflow that works.

Use rule: on a new goal, recall Experiences by goal
similarity first, before planning fresh. If a recalled
Experience matches the goal shape, its DAG nodes are the
starting plan, adapted to the new goal's constraints, and
then verified in the sandbox like any other plan. If no
Experience matches, plan fresh, and that fresh plan, if it
succeeds, becomes the next Experience.

This is the same idea the earlier design called Knowledge
(approved skills + DAGs). In the new Aidon it is called
Experience, and it is procedural memory, unified, not a
second knowledge store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .memory import Memory, Point


@dataclass
class Experience:
    goal: str
    goal_shape: str
    dag_nodes: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    outcome: str = ""
    lesson: str = ""

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
            "kind": "experience",
        }

    @classmethod
    def from_point(cls, point: Point) -> "Experience":
        payload = point.payload
        try:
            nodes = json.loads(payload.get("dag_nodes", "[]"))
        except (json.JSONDecodeError, TypeError):
            nodes = []
        return cls(
            goal=payload.get("goal", point.text),
            goal_shape=payload.get("goal_shape", ""),
            dag_nodes=nodes if isinstance(nodes, list) else [],
            tools_used=payload.get("tools_used", []),
            skills_used=payload.get("skills_used", []),
            outcome=payload.get("outcome", ""),
            lesson=payload.get("lesson", ""),
        )


def build_experience(goal: str, goal_shape: str, dag_nodes: list[dict],
                     tools_used: list[str], skills_used: list[str],
                     outcome: str, lesson: str = "") -> Experience:
    return Experience(goal=goal, goal_shape=goal_shape,
                      dag_nodes=dag_nodes, tools_used=tools_used,
                      skills_used=skills_used, outcome=outcome,
                      lesson=lesson)


def store_experience(memory: Memory, experience: Experience) -> Point:
    return memory.record("experience", experience.to_text(),
                         experience.to_payload())


def recall_experiences(memory: Memory, goal: str,
                       limit: int = 3) -> list[Experience]:
    hits = memory.recall("experience", goal, limit=limit, graph=False)
    return [Experience.from_point(hit.point) for hit in hits]
