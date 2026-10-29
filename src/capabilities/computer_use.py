"""Screenshot and computer use, post-hackathon capability, built as a tool layer, not as a live GUI driver.

What this is:
- screenshot: capture the screen to an image file, so Aidon can see what the user sees, with vision.
  Browser page screenshot already exists via Playwright MCP, this is desktop screenshot, a different thing.
- computer use: a validated action list, click, type, scroll, key, and screenshot, executed in order
  by an injected driver, with approval required for any irreversible action.

What this is not, honestly:
- There is no display in a headless run, and no mouse is moved here. The default driver raises
  "no computer-use driver configured", it never pretends to click. A real driver, on the user's own
  machine with a display, must be supplied by the caller, with the user's permission, and with a stop.
- Screenshot capture here tries PIL ImageGrab first, then falls back to an injected capture function
  in tests, and returns an error dict if neither works, never a fake image.
- Actions are validated before any execution: click needs x and y as integers in 0..10000,
  type needs non-empty text, scroll needs a delta, key needs a non-empty key name.
  Unknown actions are rejected, not skipped silently.
- Approval: irreversible actions, click, type, and key, need approve(action, detail) to return True,
  otherwise the action is not executed and the run stops with "not approved". Screenshot and scroll
  are read-only or low-risk, they do not need approval in this layer, the caller may still gate them.
"""

from __future__ import annotations

from pathlib import Path

ACTIONS = ("screenshot", "click", "type", "scroll", "key")
IRREVERSIBLE = ("click", "type", "key")


def validate_action(action: dict) -> list[str]:
    """Validate one computer-use action, return failures, empty means valid."""
    if not isinstance(action, dict):
        return ["action must be a dict"]
    kind = action.get("kind", "")
    if kind not in ACTIONS:
        return [f"unknown computer-use action: {kind}"]
    if kind == "click":
        failures = []
        for axis in ("x", "y"):
            value = action.get(axis)
            if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 10000):
                failures.append(f"click {axis} must be an integer 0..10000")
        return failures
    if kind == "type":
        return [] if isinstance(action.get("text"), str) and action["text"] else ["type needs non-empty text"]
    if kind == "scroll":
        return [] if isinstance(action.get("delta"), int) and not isinstance(action.get("delta"), bool) else ["scroll needs an integer delta"]
    if kind == "key":
        return [] if isinstance(action.get("key"), str) and action["key"] else ["key needs a non-empty key name"]
    return []


def capture_screenshot(output_path, capture=None) -> dict:
    """Capture the screen to output_path. capture, if given, is a function(output_path) -> bool, for tests and for a real driver."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if capture is not None:
        try:
            ok = capture(str(output_path))
        except Exception as exc:
            return {"ok": False, "error": f"screenshot capture failed: {type(exc).__name__}: {exc}"}
        if not ok or not output_path.exists():
            return {"ok": False, "error": "screenshot capture did not produce a file"}
        return {"ok": True, "path": str(output_path)}
    try:
        from PIL import ImageGrab
        image = ImageGrab.grab()
        image.save(output_path, "PNG")
    except Exception as exc:
        return {"ok": False, "error": f"screenshot not available in this environment: {type(exc).__name__}: {exc}. No fake screenshot is produced, a display or a capture driver is required."}
    return {"ok": True, "path": str(output_path)}


def _resolve_handles(action: dict, secrets: dict | None) -> tuple[dict, list[str]]:
    """Resolve {{secret.NAME}} handles in a type action's text.

    Secret-handle rule: the model never sees a credential
    value — it writes a handle, and the host fills the value at
    execution time. A handle is refused unless the host issued
    it, i.e. its NAME is in the secrets map the caller supplied;
    the model cannot invent a handle for an arbitrary value.
    Returns (action_to_execute, failures).
    """
    import re as _re
    resolved = dict(action)
    if action.get("kind") != "type":
        return resolved, []
    text = action.get("text", "")
    if not isinstance(text, str):
        return resolved, []
    failures = []
    for name in _re.findall(r"\{\{secret\.([A-Za-z0-9_]+)\}\}", text):
        if secrets is None or name not in secrets:
            failures.append(f"secret handle {{{{secret.{name}}}}} not issued")
    if failures:
        return resolved, failures
    for name in _re.findall(r"\{\{secret\.([A-Za-z0-9_]+)\}\}", text):
        text = text.replace(f"{{{{secret.{name}}}}}", str(secrets[name]))
    resolved["text"] = text
    return resolved, []


def _redact(text: str, secrets: dict | None) -> str:
    """Strip resolved secret values out of an error string, so a
    driver failure can never echo a credential back to the model."""
    if not secrets:
        return text
    for value in secrets.values():
        if value:
            text = text.replace(str(value), "[secret]")
    return text


def run_computer_use(actions: list, approve=None, driver=None, screenshot_dir=None, secrets=None) -> dict:
    """Run validated computer-use actions in order, with approval for irreversible actions, return executed results or an error, never a fake click.

    secrets maps handle NAME -> value for {{secret.NAME}} in
    type text. Approval sees the handle form, the driver sees
    the resolved value, and results record the handle form —
    the value never appears in a result, a trace, or an error.
    """
    if not isinstance(actions, list):
        return {"ok": False, "error": "actions must be a list"}
    results = []
    for index, action in enumerate(actions):
        failures = validate_action(action)
        if failures:
            return {"ok": False, "error": f"action {index} invalid: {'; '.join(failures)}", "executed": results}
        kind = action["kind"]
        if kind in IRREVERSIBLE:
            if approve is None or not approve(kind, str(action)):
                return {"ok": False, "error": f"action {index} not approved: {kind}", "executed": results}
        if kind == "screenshot":
            out = Path(screenshot_dir or ".") / f"screenshot_{index}.png"
            result = capture_screenshot(out, capture=action.get("capture"))
            if not result.get("ok"):
                return {"ok": False, "error": result["error"], "executed": results}
            results.append({"kind": kind, "path": result["path"]})
            continue
        resolved, handle_failures = _resolve_handles(action, secrets)
        if handle_failures:
            return {"ok": False, "error": f"action {index} invalid: {'; '.join(handle_failures)}", "executed": results}
        if driver is None:
            return {"ok": False, "error": f"no computer-use driver configured for {kind}, no GUI action was performed", "executed": results}
        try:
            driver(resolved)
        except Exception as exc:
            return {"ok": False, "error": _redact(f"driver failed on action {index} {kind}: {type(exc).__name__}: {exc}", secrets), "executed": results}
        results.append({"kind": kind, "action": action})
    return {"ok": True, "executed": results}


def run_guided_task(description: str, model_chat, approve=None, driver=None,
                    screenshot_dir=None, max_actions: int = 10,
                    capture=None, secrets=None) -> dict:
    """Observe-act computer task: screenshot, describe, decide, act.

    The pre-written action list is blind — the model writes every
    click before seeing the screen, and step results never flow
    back into planning. This loop closes that gap: each round
    captures the screen,
    asks the vision model to describe it, asks the model to
    choose the next single action (or done), then runs that
    action through the same validation + approval + secret
    handling as run_computer_use. A trace file lands next to
    the screenshots, so a proven flow can be promoted to a
    skill later. model_chat(messages) -> str; None means the
    tool refuses, it never acts blind.
    """
    from ..system.model_client import image_message
    target = Path(screenshot_dir or "computer-screenshots")
    target.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    last_description = ""
    if model_chat is None:
        return {"ok": False, "error": "guided computer task needs a model chat for vision",
                "actions_taken": history}
    if not description or not str(description).strip():
        return {"ok": False, "error": "guided computer task needs a description",
                "actions_taken": history}
    for round_no in range(max(0, int(max_actions))):
        shot = run_computer_use(
            [{"kind": "screenshot",
              **({"capture": capture} if capture is not None else {})}],
            driver=None, screenshot_dir=str(target))
        if not shot.get("ok"):
            _write_trace(target, description, history, last_description,
                         shot.get("error", "screenshot failed"))
            return {"ok": False, "error": shot.get("error", "screenshot failed"),
                    "actions_taken": history, "final_description": last_description}
        shot_path = Path(shot["executed"][0]["path"])
        # run_computer_use always writes screenshot_0.png for a
        # one-action call; keep each round's frame instead of
        # overwriting it, so the trace stays reviewable.
        framed = target / f"screenshot_round_{round_no}.png"
        try:
            shot_path.replace(framed)
        except Exception:
            framed = shot_path
        try:
            png = framed.read_bytes()
        except Exception as exc:
            return {"ok": False, "error": f"screenshot unreadable: {exc}",
                    "actions_taken": history}
        try:
            last_description = model_chat([image_message(
                "Describe this screen in 3-5 lines: focused app or window, "
                "visible text and controls, any dialog or error. "
                "No action advice.", [(png, "image/png")])])
        except Exception as exc:
            return {"ok": False, "error": f"vision failed: {exc}",
                    "actions_taken": history}
        decide_prompt = (
            f"Goal: {description}\nScreen now: {last_description}\n"
            f"Actions so far: {history}\n"
            "Reply JSON only: {\"action\": null | {\"kind\": \"click\" | \"type\" | "
            "\"scroll\" | \"key\", \"x\": int 0-10000, \"y\": int 0-10000, "
            "\"text\": str, \"delta\": int, \"key\": str}, \"done\": bool, "
            "\"reason\": str}. Set done true when the goal is achieved; "
            "otherwise give exactly one next action. A click needs x "
            "and y; type needs text; scroll needs delta; key needs key.")
        try:
            decision_raw = model_chat([{"role": "user", "content": decide_prompt}])
            decision = _parse_decision(decision_raw)
        except Exception as exc:
            _write_trace(target, description, history, last_description,
                         f"decision failed: {exc}")
            return {"ok": False, "error": f"decision failed: {exc}",
                    "actions_taken": history, "final_description": last_description}
        if decision.get("done") or decision.get("action") is None:
            _write_trace(target, description, history, last_description, "")
            return {"ok": True, "actions_taken": history,
                    "final_description": last_description,
                    "reason": decision.get("reason", "")}
        action = decision["action"]
        step = run_computer_use([action], approve=approve, driver=driver,
                                screenshot_dir=str(target), secrets=secrets)
        history.append({"description": last_description, "action": action,
                        "reason": decision.get("reason", "")})
        if not step.get("ok"):
            _write_trace(target, description, history, last_description,
                         step.get("error", "action failed"))
            return {"ok": False, "error": step.get("error", "action failed"),
                    "actions_taken": history, "final_description": last_description}
    _write_trace(target, description, history, last_description,
                 "max actions reached")
    return {"ok": False, "error": "max actions reached",
            "actions_taken": history, "final_description": last_description}


def _parse_decision(raw: str) -> dict:
    """Parse the decide reply: strict JSON, else the first {...} block."""
    import json as _json
    import re as _re
    text = str(raw or "").strip()
    try:
        decision = _json.loads(text)
    except Exception:
        match = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not match:
            raise ValueError("decision is not JSON")
        decision = _json.loads(match.group(0))
    if not isinstance(decision, dict):
        raise ValueError("decision is not an object")
    return decision


def _write_trace(target: Path, description: str, history: list,
                 last_description: str, error: str) -> None:
    """Save the flow's trace next to its screenshots, handle form
    only — resolved secret values never reach this file."""
    import json as _json
    try:
        (Path(target) / "task_trace.json").write_text(
            _json.dumps({"goal": description, "steps": history,
                         "final_description": last_description,
                         "error": error}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass
