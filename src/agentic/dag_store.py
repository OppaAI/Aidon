"""DAG library, file-based, for the WebUI DAG editor.

A DAG edited in the WebUI needs a home that is not the
model's memory alone, so the owner can open it, edit it,
and save it again, and so a saved DAG survives a memory
reset. DagStore keeps one JSON file per DAG, under
<home>/dags/<id>.json, with goal, nodes, and an edit summary.

Save rule, same as adaptation: a DAG that fails validation
is not saved as executable, it is returned with its
validation failures, so the editor can show them and the
owner can fix them. A valid DAG is saved, and, when a
memory is given, also stored as an Experience, so the
next goal run can recall it through the normal experience
path, not only through the editor.

A DAG id is a short name, letters, digits, dash, underscore
only, never a path, so a crafted id cannot escape dags/.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .dag import DAG, DAGNode

# Words that carry no goal shape: articles, prepositions,
# pronouns, and politeness fillers. Everything else in a
# goal counts toward its shape, so two goals that differ
# only in filler still match, while goals about different
# things do not.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on",
    "with", "my", "me", "i", "is", "it", "be", "by", "at", "from",
    "please", "then", "that", "this", "as", "into", "about",
})

_LEARN_SIMILARITY = 0.3


def _goal_words(goal: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(goal).lower())
            if w not in _STOPWORDS}


def _goal_slug(goal: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", str(goal).lower())
             if w not in _STOPWORDS][:4]
    slug = "-".join(words) or "goal"
    try:
        return _safe_id(slug)
    except ValueError:
        return "goal"


def _safe_id(dag_id: str) -> str:
    safe = "".join(c for c in dag_id if c.isalnum() or c in ("-", "_"))[:64]
    if not safe:
        raise ValueError("DAG id is required")
    return safe


class DagStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "dags"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, dag_id: str) -> Path:
        return self.dir / f"{_safe_id(dag_id)}.json"

    def list_dags(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                out.append({"id": path.stem, "goal": data.get("goal", ""),
                            "nodes": len(data.get("nodes", []))})
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def get(self, dag_id: str) -> dict | None:
        path = self._path(dag_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, dag_id: str, goal: str, nodes: list[dict], memory=None, merge: bool = False) -> tuple[bool, list[str]]:
        """Save a valid DAG, or return validation failures, unsaved.
        With merge=True, nodes from an existing saved DAG are kept
        unless a new node has the same id, in which case the new
        node replaces it, so a library save does not silently
        drop nodes the editor did not send. Without merge, the
        editor's list is the whole DAG, and a delete really deletes.
        """
        if merge:
            existing = self.get(dag_id)
            if existing:
                by_id = {n.get("id"): n for n in existing.get("nodes", []) if isinstance(n, dict)}
                for node in nodes:
                    if isinstance(node, dict) and node.get("id"):
                        by_id[node["id"]] = node
                nodes = list(by_id.values())
        try:
            dag = DAG([DAGNode.from_dict(node) for node in nodes])
        except Exception as exc:
            return False, [f"invalid node: {exc}"]
        failures = dag.validate()
        if failures:
            return False, failures
        data = {"id": _safe_id(dag_id), "goal": goal,
                "nodes": [node.to_dict() for node in dag.topological_order()]}
        self._path(dag_id).write_text(json.dumps(data, indent=2))
        if memory is not None:
            try:
                from ..memory.experience import build_experience, store_experience
                experience = build_experience(
                    goal, "", [{"tool": n.tool, "args": n.args, "deliverable": n.deliverable,
                                "assumptions": n.assumptions} for n in dag.topological_order()],
                    sorted({n.tool for n in dag.topological_order()}), [],
                    "Saved from the DAG editor.")
                store_experience(memory, experience)
            except Exception:
                pass
        return True, []

    def learn(self, goal: str, nodes: list) -> tuple[str, str]:
        """Learn a proven run's DAG into the library.

        One DAG per goal shape: the new goal's content words
        are compared (Jaccard similarity) against every saved
        DAG's goal. At 0.3 or above, the best match is
        REPLACED under its existing id — no near-duplicate
        is added. Below that, the DAG is saved under a new
        slug id derived from the goal, so distinct shapes
        coexist. Returns ("replaced", id), ("added", id),
        or ("invalid", "") when the nodes cannot form a
        valid DAG. This is the learn path only; the editor
        path (save) is untouched.
        """
        norm = []
        for index, node in enumerate(nodes or []):
            if isinstance(node, dict):
                item = {"id": f"step-{index + 1}",
                        "tool": str(node.get("tool", "write")),
                        "args": dict(node.get("args", {})) if isinstance(node.get("args", {}), dict) else {},
                        "deliverable": str(node.get("deliverable", "")),
                        "depends_on": list(node.get("depends_on", [])) if isinstance(node.get("depends_on", []), list) else []}
            else:
                item = {"id": f"step-{index + 1}",
                        "tool": str(getattr(node, "tool", "write")),
                        "args": dict(getattr(node, "args", {}) or {}),
                        "deliverable": str(getattr(node, "deliverable", "")),
                        "depends_on": []}
            norm.append(item)
        if not norm:
            return ("invalid", "")
        words = _goal_words(goal)
        best_id, best_score = "", 0.0
        for entry in self.list_dags():
            saved = self.get(entry["id"])
            if saved is None:
                continue
            other = _goal_words(saved.get("goal", ""))
            if not words and not other:
                score = 1.0
            elif not words or not other:
                score = 0.0
            else:
                score = len(words & other) / len(words | other)
            if score > best_score:
                best_id, best_score = entry["id"], score
        if best_id and best_score >= _LEARN_SIMILARITY:
            ok, _failures = self.save(best_id, goal, norm)
            return ("replaced", best_id) if ok else ("invalid", "")
        slug = _goal_slug(goal)
        candidate = slug
        suffix = 2
        while self.get(candidate) is not None:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        ok, _failures = self.save(candidate, goal, norm)
        return ("added", candidate) if ok else ("invalid", "")

    def delete(self, dag_id: str) -> bool:
        path = self._path(dag_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
