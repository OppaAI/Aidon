"""Command line for Aidon, skeleton version.

Usage:
  python -m aidon.main goal "Plan a dinner for two"
  python -m aidon.main --help

Configuration comes from the environment only:
  AIDON_HOME      where Aidon keeps its state (default ~/.aidon)
  AIDON_MODEL     model name at the endpoint (default muse-spark)
  AIDON_API_BASE  endpoint URL for the model API
  AIDON_API_KEY   API key for that endpoint

No vendor-specific client, no hard-coded endpoint, no key in
code or in chat. The model is just an endpoint, a key, and a
model name. In the demo, that endpoint is the Meta Model API.
"""

from __future__ import annotations

import argparse
import sys

from .approval import ApprovalGate, Decision
from .config import Config
from .loop import GoalRunner
from .model_client import FakeClient, HttpModelClient
from .sandbox import Sandbox


def _cli_gate() -> ApprovalGate:
    def decide(kind: str, title: str, body: str) -> Decision:
        print(f"\n[{kind}] {title}\n{body}\n")
        answer = input("Approve? [y/N] ").strip().lower()
        return Decision("approved" if answer in ("y", "yes") else "rejected")

    return ApprovalGate(decide)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidon")
    sub = parser.add_subparsers(dest="command", required=True)
    goal_parser = sub.add_parser("goal", help="Run a goal")
    goal_parser.add_argument("text", help="Goal in plain language")
    args = parser.parse_args(argv)

    config = Config.load()
    config.ensure_home()
    if config.api_base:
        client = HttpModelClient(config.api_base, config.api_key,
                                 config.model)
    else:
        # No endpoint configured: run with a safe default plan so
        # the skeleton still proves sandbox, verify, and approval
        # without making a model call. This is not a demo run.
        client = FakeClient("")
    sandbox = Sandbox(config.workdir, config.sandbox_timeout_seconds)
    runner = GoalRunner(client, sandbox, _cli_gate(), config.home)
    report = runner.run(args.text)
    print(f"status: {report.status}")
    print(f"verified: {report.verified}")
    print(f"approved: {report.approved}")
    print(f"files: {', '.join(report.files)}")
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
