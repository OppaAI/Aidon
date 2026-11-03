"""Approval-gated desktop and computer-use tools for goal runs."""

from __future__ import annotations

import json
from copy import deepcopy
import os


def register_computer_tools(config, tools, approve, confined, model_chat=None, on_action=None,
                            approved_tasks=None):
    """Register X11 actions and the optional vision-guided computer task."""
    _approval = approve
    _confined = confined
    grants = deepcopy(approved_tasks or [])

    def _scope(tool, args):
        values = dict(args)
        values.setdefault("screenshot_dir", "computer-screenshots")
        if tool == "computer_use_task":
            values["max_actions"] = min(30, max(0, int(values.get("max_actions", 10))))
        return {"tool": tool, "args": values}

    def _authorize(driver, tool, args, detail):
        scope = _scope(tool, args)
        for index, grant in enumerate(grants):
            if _scope(grant["tool"], grant["args"]) == scope:
                grants.pop(index)
                return None
        # Approval moves focus to the browser. Restore the original window
        # once, before observing or controlling the desktop.
        window = driver.capture_focus() if hasattr(driver, "capture_focus") else None
        ok, why = _approval(
            "live_execute", "Allow desktop control for this task",
            detail + "\nThis authorizes the whole task, without approval for each click or keystroke.",
            force_ask=True)
        if not ok:
            return {"ok": False, "error": f"Desktop task not approved: {why}"}
        if window is not None and hasattr(driver, "restore_focus"):
            driver.restore_focus(window)
        return None

    def _computer_secrets() -> dict:
        """Secrets the owner authorized for computer use.

        AIDON_COMPUTER_SECRETS lists env names (kept in .env, which
        Config already loads into os.environ); only listed names
        resolve. The model names a handle, never a value, and an
        unlisted handle is refused inside run_computer_use.
        """
        names = os.environ.get("AIDON_COMPUTER_SECRETS", "")
        secrets = {}
        for name in names.split(","):
            name = name.strip()
            if name and name in os.environ:
                secrets[name] = os.environ[name]
        return secrets

    def _guided_computer_task(driver):
        """computer_use_task closure over one driver + approval path.

        Only wired when a model chat is available: the guided
        loop needs vision, and without it the tool would act
        blind. Scheduled runs never pass model_chat, so they
        never get this tool.
        """
        def computer_use_task(description, max_actions=10,
                              screenshot_dir="computer-screenshots"):
            from ..capabilities.computer_use import run_guided_task
            target = _confined(screenshot_dir)
            max_actions = min(30, max(0, int(max_actions)))
            refusal = _authorize(driver, "computer_use_task", {
                "description": description, "max_actions": max_actions,
                "screenshot_dir": screenshot_dir},
                f"Desktop task (up to {max_actions} actions):\n{description}")
            if refusal:
                return json.dumps(refusal)
            result = run_guided_task(
                description, model_chat, approve=lambda *_: True,
                driver=driver, screenshot_dir=target,
                max_actions=max_actions, secrets=_computer_secrets(), on_action=on_action)
            return json.dumps(result, ensure_ascii=False)
        return computer_use_task

    # The WebUI goal runner receives this dictionary as its extra toolset.
    # Keep desktop control here as well as in the CLI tool builder; otherwise
    # the status panel can say x11 while the planner cannot select
    # computer_use for a WebUI goal.
    # Prefer explicit configuration, but also discover an installed X11
    # driver automatically. Discovery is read-only; every actual desktop
    # action remains approval-gated below.
    _computer_requested = getattr(config, "computer_driver", "") == "x11"
    if not getattr(config, "kernel_mode", False) and (_computer_requested or
                                                       os.environ.get("DISPLAY")):
        try:
            from ..capabilities.computer_use import run_computer_use
            from ..capabilities.x11_driver import X11Driver, x11_available
            if not _computer_requested and not x11_available():
                raise RuntimeError("no discoverable X11 driver")
            desktop_driver = X11Driver()

            def computer_use(actions, screenshot_dir="computer-screenshots"):
                target = _confined(screenshot_dir)
                from ..capabilities.computer_use import validate_action, IRREVERSIBLE
                if not isinstance(actions, list):
                    return json.dumps({"ok": False, "error": "actions must be a list"})
                for action in actions:
                    failures = validate_action(action)
                    if failures:
                        return json.dumps({"ok": False, "error": "; ".join(failures)})
                if any(action["kind"] in IRREVERSIBLE for action in actions):
                    refusal = _authorize(desktop_driver, "computer_use", {
                        "actions": actions, "screenshot_dir": screenshot_dir},
                        f"Execute these {len(actions)} desktop actions:\n" + json.dumps(actions))
                    if refusal:
                        return json.dumps(refusal)
                result = run_computer_use(
                    actions, approve=lambda *_: True, driver=desktop_driver,
                    screenshot_dir=target, secrets=_computer_secrets(), on_action=on_action)
                return json.dumps(result, ensure_ascii=False)

            tools["computer_use"] = computer_use
            if model_chat is not None:
                tools["computer_use_task"] = _guided_computer_task(
                    desktop_driver)
        except Exception:
            # A missing xdotool or DISPLAY must not make other tools vanish.
            pass
