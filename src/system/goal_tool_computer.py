"""Approval-gated desktop and computer-use tools for goal runs."""

from __future__ import annotations

import json
import os


def register_computer_tools(config, tools, approve, confined, model_chat=None):
    """Register X11 actions and the optional vision-guided computer task."""
    _approval = approve
    _confined = confined
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

    def _guided_computer_task(driver, approve_fn):
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
            result = run_guided_task(
                description, model_chat, approve=approve_fn,
                driver=driver, screenshot_dir=target,
                max_actions=max_actions, secrets=_computer_secrets())
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

            def _desktop_approve(kind, detail):
                ok, _ = _approval(
                    "live_execute", "Approve desktop action",
                    f"Run X11 computer action {kind}: {detail}",
                    force_ask=True,
                )
                return ok

            def computer_use(actions, screenshot_dir="computer-screenshots"):
                target = _confined(screenshot_dir)
                result = run_computer_use(
                    actions, approve=_desktop_approve, driver=desktop_driver,
                    screenshot_dir=target, secrets=_computer_secrets())
                return json.dumps(result, ensure_ascii=False)

            tools["computer_use"] = computer_use
            if model_chat is not None:
                tools["computer_use_task"] = _guided_computer_task(
                    desktop_driver, _desktop_approve)
        except Exception:
            # A missing xdotool or DISPLAY must not make other tools vanish.
            pass

    # Desktop control is opt-in and never a raw shell. Kernel mode keeps it
    # unavailable; the demo must explicitly run with AIDON_KERNEL=0.
    if getattr(config, "computer_driver", "") == "x11" and not getattr(config, "kernel_mode", False):
        try:
            from ..capabilities.computer_use import run_computer_use
            from ..capabilities.x11_driver import X11Driver

            _desktop_driver = X11Driver()

            def _desktop_approve2(kind, detail):
                ok, why = _approval(
                    "live_execute", "Approve desktop action",
                    f"Run X11 computer action {kind}: {detail}",
                    force_ask=True,
                )
                return ok

            def computer_use(actions, screenshot_dir="computer-screenshots"):
                target = _confined(screenshot_dir)
                result = run_computer_use(
                    actions, approve=_desktop_approve2, driver=_desktop_driver,
                    screenshot_dir=target, secrets=_computer_secrets(),
                )
                return json.dumps(result, ensure_ascii=False)

            tools["computer_use"] = computer_use
            if model_chat is not None:
                tools["computer_use_task"] = _guided_computer_task(
                    _desktop_driver, _desktop_approve2)
        except Exception:
            # A missing display or driver leaves the capability unavailable.
            pass

