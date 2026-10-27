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
from pathlib import Path

from .dag import DAG, DAGNode


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

    def save(self, dag_id: str, goal: str, nodes: list[dict], memory=None) -> tuple[bool, list[str]]:
        """Save a valid DAG, or return validation failures, unsaved."""
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
                from .experience import build_experience, store_experience
                experience = build_experience(
                    goal, "", [{"tool": n.tool, "args": n.args, "deliverable": n.deliverable,
                                "assumptions": n.assumptions} for n in dag.topological_order()],
                    sorted({n.tool for n in dag.topological_order()}), [],
                    "Saved from the DAG editor.")
                store_experience(memory, experience)
            except Exception:
                pass
        return True, []

    def delete(self, dag_id: str) -> bool:
        path = self._path(dag_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
