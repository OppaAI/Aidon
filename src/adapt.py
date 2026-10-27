"""Adapt a saved workflow to a new goal, instead of replaying it blindly.

The review that prompted this module was right: replaying a Snake
workflow verbatim does not reliably make Worm. A saved DAG node
mixes two things: reusable logic (create a game loop, handle input,
run tests) and task-specific arguments (the word Snake, the file
snake.py, the text "Snake"). This module separates them.

Generalization, no model call:
- generalize_nodes(nodes, old_goal) replaces the old goal's
  distinctive words in node args and deliverables with a
  {task} placeholder. The result is a reusable template, not
  a Snake plan. It is stored alongside the Experience, so a
  future run can instantiate it without re-planning.
- instantiate_template(template, new_goal) fills {task} with
  the new goal's distinctive word (Worm for Snake -> Worm),
  and renames deliverables the same way.

Adaptation, one model call at most, only for changed nodes:
- adapt_experience(experience, new_goal, client=None) returns
  adapted nodes, or None when the shape does not match and a
  fresh plan is safer. Identical goal: nodes unchanged, no
  model call. Similar shape: try one model call that returns
  only the changed nodes as JSON; unchanged nodes keep their
  verified args verbatim, never re-planned. Model failure,
  unparseable reply, or no client: heuristic fallback, the
  noun replacement above, no model call.
- adapt_tools_skills(experience, new_goal): tools are generic
  (write, read, list, shell, calculate), so they stay the same.
  Skill names that carry the old task word are renamed the
  same way as deliverables; a skill tied to the old task by
  name does not silently follow the new task under the old name.
"""

from __future__ import annotations

import json
import re

from .embedder import tokens
from .experience import goal_shape_for

_STOP = {"a", "an", "the", "for", "to", "and", "or", "of", "in", "on",
         "with", "i", "want", "please", "me", "my", "write", "make",
         "create", "build", "game", "app", "note", "file", "a"}


def distinctive_words(goal: str) -> list[str]:
    """Words in a goal that are not stopwords and not generic verbs."""
    seen = []
    for word in tokens(goal):
        if word not in _STOP and word not in seen:
            seen.append(word)
    return seen


def _word_map(old_goal: str, new_goal: str) -> dict[str, str]:
    """Map old distinctive words to new ones, by position.

    Snake -> Worm: old distinctive [snake], new [worm], map
    snake -> worm. Longer goals map positionally, first to
    first, and only for words that actually changed.
    """
    old_words = distinctive_words(old_goal)
    new_words = distinctive_words(new_goal)
    mapping = {}
    for old, new in zip(old_words, new_words):
        if old != new:
            mapping[old] = new
    return mapping


def _replace_word(text: str, mapping: dict[str, str]) -> str:
    out = text
    for old, new in mapping.items():
        # Case-preserving, word-boundary replace, both cases.
        out = re.sub(rf"\b{re.escape(old)}\b", new, out,
                     flags=re.IGNORECASE)
        # Restore capitalised form where the old word was capitalised.
        out = re.sub(rf"\b{new.capitalize()}\b", new.capitalize(), out) \
            if old[0].isupper() else out
    return out


def _replace_in_args(args: dict, mapping: dict[str, str]) -> dict:
    out = {}
    for key, value in args.items():
        if isinstance(value, str):
            out[key] = _replace_word(value, mapping)
        else:
            out[key] = value
    return out


def generalize_nodes(nodes: list[dict], old_goal: str) -> list[dict]:
    """Replace old task words with {task} in args and deliverables."""
    words = distinctive_words(old_goal)
    out = []
    for node in nodes:
        new_node = dict(node)
        args = dict(node.get("args", {}))
        for key, value in args.items():
            if isinstance(value, str):
                for word in words:
                    value = re.sub(rf"\b{re.escape(word)}\b", "{task}",
                                   value, flags=re.IGNORECASE)
                args[key] = value
        new_node["args"] = args
        deliverable = str(node.get("deliverable", ""))
        for word in words:
            deliverable = re.sub(rf"\b{re.escape(word)}\b", "{task}",
                                 deliverable, flags=re.IGNORECASE)
        if deliverable:
            new_node["deliverable"] = deliverable
        out.append(new_node)
    return out


def instantiate_template(template: list[dict], new_goal: str) -> list[dict]:
    """Fill a {task} template with the new goal's distinctive word."""
    words = distinctive_words(new_goal)
    task = words[0] if words else "task"
    out = []
    for node in template:
        new_node = dict(node)
        args = {}
        for key, value in node.get("args", {}).items():
            args[key] = value.replace("{task}", task) \
                .replace("{Task}", task.capitalize()) if isinstance(value, str) else value
        new_node["args"] = args
        if node.get("deliverable"):
            new_node["deliverable"] = str(node["deliverable"]) \
                .replace("{task}", task).replace("{Task}", task.capitalize())
        out.append(new_node)
    return out


def adapt_tools_skills(experience, new_goal: str) -> tuple[list[str], list[str]]:
    """Tools stay generic; skill names carrying the old word are renamed."""
    mapping = _word_map(experience.goal, new_goal)
    skills = [_replace_word(skill, mapping) for skill in experience.skills_used]
    return list(experience.tools_used), skills


def adapt_experience(experience, new_goal: str, client=None):
    """Adapt a saved Experience to a new goal.

    Returns (nodes, adapted_count, model_calls) or None when
    the shape does not match and a fresh plan is safer.
    Identical goal: (nodes unchanged, 0, 0). Similar shape:
    one model call at most, only changed nodes, else heuristic.
    """
    old_goal = experience.goal
    if old_goal == new_goal:
        return list(experience.dag_nodes), 0, 0
    shape_old = goal_shape_for(old_goal)
    shape_new = goal_shape_for(new_goal)
    set_old, set_new = set(shape_old.split()), set(shape_new.split())
    overlap = len(set_old & set_new) / len(set_old | set_new) if (set_old | set_new) else 0.0
    if overlap < 0.3:
        return None
    # Heuristic first, so a model failure still gives a usable plan.
    mapping = _word_map(old_goal, new_goal)
    heuristic = []
    for node in experience.dag_nodes:
        new_node = dict(node)
        new_node["args"] = _replace_in_args(dict(node.get("args", {})), mapping)
        if node.get("deliverable"):
            new_node["deliverable"] = _replace_word(str(node["deliverable"]), mapping)
        heuristic.append(new_node)
    if client is None:
        changed = sum(1 for a, b in zip(heuristic, experience.dag_nodes) if a != b)
        return heuristic, changed, 0
    # One selective model call: only changed nodes, as JSON.
    from .model_client import Message
    prompt = (
        f"Old goal: {old_goal}\nNew goal: {new_goal}\n"
        f"Old workflow nodes, as JSON: {json.dumps(experience.dag_nodes)}\n"
        "Return JSON only, with key 'adapted', a list of objects "
        "with 'index' and 'args' and 'deliverable', for nodes whose "
        "args or deliverable must change for the new goal. "
        "Do not return unchanged nodes. Do not invent new tools.")
    try:
        reply = client.chat([Message("system", "Adapt only changed workflow nodes, JSON only."),
                             Message("user", prompt)])
        data = json.loads(reply)
        adapted = data.get("adapted", []) if isinstance(data, dict) else []
        if not isinstance(adapted, list):
            return heuristic, sum(1 for a, b in zip(heuristic, experience.dag_nodes) if a != b), 1
        out = [dict(node) for node in experience.dag_nodes]
        changed = 0
        for item in adapted:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(out):
                if "args" in item:
                    out[idx]["args"] = item["args"]
                if "deliverable" in item:
                    out[idx]["deliverable"] = item["deliverable"]
                changed += 1
        if changed == 0:
            return heuristic, sum(1 for a, b in zip(heuristic, experience.dag_nodes) if a != b), 1
        return out, changed, 1
    except Exception:
        changed = sum(1 for a, b in zip(heuristic, experience.dag_nodes) if a != b)
        return heuristic, changed, 1
