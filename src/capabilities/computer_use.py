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


def run_computer_use(actions: list, approve=None, driver=None, screenshot_dir=None) -> dict:
    """Run validated computer-use actions in order, with approval for irreversible actions, return executed results or an error, never a fake click."""
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
        if driver is None:
            return {"ok": False, "error": f"no computer-use driver configured for {kind}, no GUI action was performed", "executed": results}
        try:
            driver(action)
        except Exception as exc:
            return {"ok": False, "error": f"driver failed on action {index} {kind}: {type(exc).__name__}: {exc}", "executed": results}
        results.append({"kind": kind, "action": action})
    return {"ok": True, "executed": results}
