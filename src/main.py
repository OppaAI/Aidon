"""Command line for Aidon.

Usage:
  python -m src.main goal "Plan a dinner for two"
  python -m src.main goal --project dinner "Plan a dinner for two"

Configuration comes from the environment, or from a .env
file in the current directory or in the Aidon home, loaded
without overriding real environment variables:
  AIDON_HOME      where Aidon keeps its state (default ~/.aidon)
  AIDON_MODEL     model name at the endpoint (default muse-spark)
  AIDON_API_BASE  endpoint URL for the model API
  AIDON_API_KEY   API key for that endpoint
  AIDON_EMBED_MODEL  embedding model, default google/embeddinggemma-2
  AIDON_EMBEDDER=hash  forces the test embedder

Memory starts blank, by design: new collections, no import
from previous projects. If the real embedder is not
installed, the CLI falls back to HashEmbedder with a
warning on stderr, so a run still works offline, with
non-semantic recall. The demo and submission run on the
real embedder, not on the fallback.
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


def _promotion_ask(point) -> bool:
    answer = input(
        f"\nPromote this run's episodic memory to long-term memory? "
        f"[y/N] ").strip().lower()
    return answer in ("y", "yes")


def _make_memory(config: Config):
    """Memory with the real embedder if available, else Hash, with warning."""
    try:
        from .embedder import HashEmbedder, make_embedder_cached
        from .memory import Memory
        embedder = make_embedder_cached()
        return Memory(config.home, embedder=embedder)
    except Exception as exc:
        print(f"warning: real embedder unavailable ({exc}), "
              "using HashEmbedder for this run", file=sys.stderr)
        from .embedder import HashEmbedder
        from .memory import Memory
        return Memory(config.home, embedder=HashEmbedder())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidon")
    sub = parser.add_subparsers(dest="command", required=True)
    goal_parser = sub.add_parser("goal", help="Run a goal")
    goal_parser.add_argument("text", help="Goal in plain language")
    goal_parser.add_argument("--project", default="default",
                             help="Project id for episodic memory")
    args = parser.parse_args(argv)

    config = Config.load()
    config.ensure_home()
    if config.api_base:
        client = HttpModelClient(config.api_base, config.api_key,
                                 config.model)
    else:
        client = FakeClient("")
    sandbox = Sandbox(config.workdir, config.sandbox_timeout_seconds)
    memory = _make_memory(config)
    runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                        memory=memory, promotion_ask=_promotion_ask,
                        project_id=args.project)
    report = runner.run(args.text)
    print(f"status: {report.status}")
    print(f"verified: {report.verified}")
    print(f"approved: {report.approved}")
    print(f"files: {', '.join(report.files)}")
    print(f"attempts: {report.attempts}")
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
