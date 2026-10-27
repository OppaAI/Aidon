"""Adaptive DAG: Aidon edits its own workflow, by task, not by tool list.

A stored Experience, until now, was a sequential list of tool
steps. A DAG is more than that: each node is a contract, with
inputs, outputs, preconditions, an expected result, and
dependencies on other nodes, and the graph must stay acyclic.

Node contract:
- id: stable, short, for example "setup", "logic", "tests".
- tool, args, deliverable: what the node does, as before.
- depends_on: ids of nodes that must run first.
- inputs, outputs: files or facts the node reads and writes.
- preconditions: conditions that must hold before the node runs.
- expected: the result the node must produce, for verification.
- assumptions: conditions the node assumes about the task,
  for example "game=snake". A node is reused only when its
  assumptions still hold for the new goal. That is evidence-
  based reuse, not replay: a Snake-only node, whose
  assumption is game=snake, is removed for Worm, not renamed.

Graph edits, all validated before execution:
- add(node): a new node, with dependencies that exist.
- remove(node_id): removes the node, and rewires dependents
  to the removed node's dependencies, so the graph stays connected.
- modify(node_id, args=None, deliverable=None, depends_on=None):
  changes a node in place, never its id.
- reorder(node_id, before_id): moves a node earlier, by
  changing dependencies, only if the move keeps the graph acyclic.
- validate(): missing dependency, self-dependency, duplicate
  id, duplicate deliverable, and cycle, are all failures.
- topological_order(): the execution order, dependencies first.
  A cycle is a failure, never silently broken by dropping a node.

Adaptation, Snake to Worm, in five steps, as specified:
1. Understand the goal: deliverables, constraints, and the
   new task word, from the goal text, not from a tool list.
2. Retrieve and adapt: start from the saved DAG, keep nodes
   whose assumptions hold, modify nodes whose args carry the
   old task word, remove nodes whose assumptions fail, add a
   test node for the new task word if none exists, and rewire
   dependencies so tests depend on the logic they test.
3. Validate, before execution. If the adapted graph fails
   validation, roll back to the original saved graph, and if
   that also fails, to a fresh plan. A modified graph is
   never executed unvalidated.
4. Execute and repair, in the loop: a failed node is replaced
   or repaired, not the whole graph rebuilt, and passed nodes
   are untouched, as the loop already does for deliverables.
5. Learn: the successful adapted graph, with its edit
   summary (kept, modified, added, removed, reordered), is
   saved as the next Experience, so the next task starts
   from the improved graph, not from Snake again.

Safety boundary: graph edits are plan edits, in the sandbox.
A modified graph still goes through code verification and
the approval gate before any live action. Changing a plan
does not change a permission, and an external side effect
still needs its approval card.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .adapt import _replace_in_args, _replace_word, _word_map, distinctive_words


@dataclass
class DAGNode:
    id: str
    tool: str = "write"
    args: dict = field(default_factory=dict)
    deliverable: str = ""
    depends_on: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected: str = ""
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": self.args,
                "deliverable": self.deliverable, "depends_on": list(self.depends_on),
                "inputs": list(self.inputs), "outputs": list(self.outputs),
                "preconditions": list(self.preconditions), "expected": self.expected,
                "assumptions": list(self.assumptions)}

    @classmethod
    def from_dict(cls, data: dict) -> "DAGNode":
        return cls(id=str(data.get("id", "")),
                   tool=str(data.get("tool", "write")),
                   args=dict(data.get("args", {})),
                   deliverable=str(data.get("deliverable", "")),
                   depends_on=list(data.get("depends_on", [])),
                   inputs=list(data.get("inputs", [])),
                   outputs=list(data.get("outputs", [])),
                   preconditions=list(data.get("preconditions", [])),
                   expected=str(data.get("expected", "")),
                   assumptions=list(data.get("assumptions", [])))


class DAG:
    def __init__(self, nodes: list[DAGNode] | None = None):
        self.nodes: dict[str, DAGNode] = {}
        for node in nodes or []:
            self.nodes[node.id] = node

    def snapshot(self) -> "DAG":
        return DAG([copy.deepcopy(node) for node in self.nodes.values()])

    def add(self, node: DAGNode) -> None:
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id: {node.id}")
        self.nodes[node.id] = node

    def remove(self, node_id: str) -> DAGNode:
        if node_id not in self.nodes:
            raise ValueError(f"no such node: {node_id}")
        removed = self.nodes.pop(node_id)
        # Rewire dependents to the removed node's dependencies,
        # so the graph stays connected and acyclic if it was.
        for node in self.nodes.values():
            if node_id in node.depends_on:
                node.depends_on = [d for d in node.depends_on if d != node_id]
                for dep in removed.depends_on:
                    if dep not in node.depends_on and dep in self.nodes:
                        node.depends_on.append(dep)
        return removed

    def modify(self, node_id: str, args: dict | None = None,
               deliverable: str | None = None, depends_on: list[str] | None = None) -> None:
        if node_id not in self.nodes:
            raise ValueError(f"no such node: {node_id}")
        node = self.nodes[node_id]
        if args is not None:
            node.args = args
        if deliverable is not None:
            node.deliverable = deliverable
        if depends_on is not None:
            node.depends_on = list(depends_on)

    def reorder(self, node_id: str, before_id: str) -> None:
        """Move node_id before before_id, by dependency change, if acyclic."""
        if node_id not in self.nodes or before_id not in self.nodes:
            raise ValueError("reorder needs two existing nodes")
        target = self.nodes[before_id]
        if node_id not in target.depends_on:
            target.depends_on.append(node_id)
        # Remove the reverse dependency, if it existed, so the move is real.
        if before_id in self.nodes[node_id].depends_on:
            self.nodes[node_id].depends_on.remove(before_id)
        failures = self.validate()
        if failures:
            raise ValueError(f"reorder would break the graph: {'; '.join(failures)}")

    def validate(self) -> list[str]:
        failures = []
        seen_deliverables: dict[str, str] = {}
        for node in self.nodes.values():
            if node.id in node.depends_on:
                failures.append(f"node {node.id} depends on itself")
            for dep in node.depends_on:
                if dep not in self.nodes:
                    failures.append(f"node {node.id} depends on missing {dep}")
            if node.deliverable:
                if node.deliverable in seen_deliverables:
                    failures.append(f"duplicate deliverable {node.deliverable}")
                seen_deliverables[node.deliverable] = node.id
        # Cycle check, via topological sort.
        try:
            self.topological_order()
        except ValueError as exc:
            failures.append(str(exc))
        return failures

    def topological_order(self) -> list[DAGNode]:
        order: list[DAGNode] = []
        visited: dict[str, str] = {}
        def visit(node_id: str):
            state = visited.get(node_id)
            if state == "done":
                return
            if state == "visiting":
                raise ValueError(f"cycle in DAG at {node_id}")
            visited[node_id] = "visiting"
            for dep in self.nodes[node_id].depends_on:
                if dep in self.nodes:
                    visit(dep)
            visited[node_id] = "done"
            order.append(self.nodes[node_id])
        for node_id in list(self.nodes):
            visit(node_id)
        return order

    def to_plan_steps(self) -> list[dict]:
        return [{"tool": node.tool, "args": node.args, "deliverable": node.deliverable}
                for node in self.topological_order()]


def dag_from_experience(experience) -> DAG:
    """Convert a saved sequential Experience to a DAG, chain dependencies."""
    nodes = []
    previous = None
    for index, step in enumerate(experience.dag_nodes):
        from pathlib import Path as _Path
        node_id = step.get("id") or _Path(str(step.get("deliverable", ""))).stem or f"step-{index}"
        node = DAGNode(id=node_id, tool=str(step.get("tool", "write")),
                       args=dict(step.get("args", {})),
                       deliverable=str(step.get("deliverable", "")),
                       depends_on=[previous] if previous else [],
                       inputs=list(step.get("inputs", [])),
                       outputs=[str(step.get("deliverable", ""))] if step.get("deliverable") else [],
                       expected=str(step.get("expected", "")),
                       assumptions=list(step.get("assumptions", [])))
        nodes.append(node)
        previous = node_id
    return DAG(nodes)


def _assumption_holds(assumption: str, old_goal: str, new_goal: str) -> bool:
    """An assumption like 'game=snake' holds only if the new goal still has snake."""
    if "=" in assumption:
        _key, value = assumption.split("=", 1)
        return value.lower() in new_goal.lower()
    return assumption.lower() in new_goal.lower()


def adapt_dag(experience, new_goal: str, client=None):
    """Adapt a saved DAG to a new goal, validated, with rollback.

    Returns (dag, summary, model_calls) or None when the shape
    does not match and a fresh plan is safer. On any validation
    failure, the original saved DAG is returned, not a broken
    graph, and the summary says rolled_back. That rollback is
    the safety rule: a modified graph is never executed unvalidated.
    """
    from ..memory.experience import goal_shape_for
    old_goal = experience.goal
    if old_goal == new_goal:
        dag = dag_from_experience(experience)
        return dag, {"kept": [n.id for n in dag.nodes.values()], "modified": [],
                     "added": [], "removed": [], "reordered": [], "rolled_back": False}, 0
    shape_old = goal_shape_for(old_goal)
    shape_new = goal_shape_for(new_goal)
    set_old, set_new = set(shape_old.split()), set(shape_new.split())
    overlap = len(set_old & set_new) / len(set_old | set_new) if (set_old | set_new) else 0.0
    if overlap < 0.3:
        return None
    original = dag_from_experience(experience)
    dag = original.snapshot()
    mapping = _word_map(old_goal, new_goal)
    kept, modified, removed = [], [], []
    # Evidence-based reuse: remove nodes whose assumptions fail,
    # modify nodes whose args carry the old task word, keep the rest.
    for node_id in list(dag.nodes):
        node = dag.nodes[node_id]
        if node.assumptions and not all(_assumption_holds(a, old_goal, new_goal) for a in node.assumptions):
            dag.remove(node_id)
            removed.append(node_id)
            continue
        new_args = _replace_in_args(dict(node.args), mapping)
        new_deliverable = _replace_word(str(node.deliverable), mapping) if node.deliverable else ""
        if new_args != node.args or new_deliverable != node.deliverable:
            dag.modify(node_id, args=new_args, deliverable=new_deliverable)
            modified.append(node_id)
        else:
            kept.append(node_id)
    # Add a test node for the new task word, if none exists.
    new_words = distinctive_words(new_goal)
    task = new_words[0] if new_words else "task"
    added = []
    if not any(task in (node.deliverable or "").lower() and "test" in (node.deliverable or "").lower()
               for node in dag.nodes.values()):
        logic_id = None
        for node in dag.nodes.values():
            if node.tool == "write" and task in (node.deliverable or "").lower():
                logic_id = node.id
                break
        test_id = f"{task}_tests"
        if test_id not in dag.nodes:
            dag.add(DAGNode(id=test_id, tool="write",
                            args={"file": f"{task}_test.py", "text": f"Tests for {task} behavior.\n"},
                            deliverable=f"{task}_test.py",
                            depends_on=[logic_id] if logic_id else [],
                            inputs=[f"{task}.py"] if logic_id else [],
                            outputs=[f"{task}_test.py"],
                            expected=f"{task}_test.py exists"))
            added.append(test_id)
    # Model planner edit, one call at most, only if a client is given.
    model_calls = 0
    if client is not None:
        try:
            import json as _json
            from ..core.model_client import Message
            _msgs = [Message("system", "Return JSON only, with keys keep, modify, add, remove, each a list of node ids or, for add, node objects."),
                     Message("user", f"Old goal: {old_goal}\nNew goal: {new_goal}\nNodes: {_json.dumps([n.to_dict() for n in dag.nodes.values()])}")]
            reply = client.chat_phase("adapt", _msgs) if hasattr(client, "chat_phase") else client.chat(_msgs)
            model_calls = 1
            data = _json.loads(reply)
            if isinstance(data, dict):
                for node_id in data.get("remove", []):
                    if node_id in dag.nodes and node_id not in removed:
                        dag.remove(node_id)
                        removed.append(node_id)
                for item in data.get("add", []):
                    if isinstance(item, dict) and item.get("id") and item["id"] not in dag.nodes:
                        dag.add(DAGNode.from_dict(item))
                        added.append(item["id"])
        except Exception:
            pass
    failures = dag.validate()
    if failures:
        return original, {"kept": [n.id for n in original.nodes.values()], "modified": [],
                          "added": [], "removed": [], "reordered": [],
                          "rolled_back": True, "validation_failures": failures}, model_calls
    summary = {"kept": kept, "modified": modified, "added": added, "removed": removed,
               "reordered": [n.id for n in dag.topological_order()], "rolled_back": False}
    return dag, summary, model_calls
