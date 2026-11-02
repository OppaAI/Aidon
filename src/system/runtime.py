"""Runtime construction helpers shared by CLI and service startup."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

from ..interface.approval import ApprovalGate, Decision
from ..capabilities.sandbox import Sandbox
from .config import Config
from .goal_tools import _deep_search_fns, _env_int

def _startup_style(kind: str, text: str) -> None:
    """Print compact, terminal-friendly startup diagnostics."""
    if sys.stderr.isatty():
        colors = {
            "section": "\033[38;5;141m\033[1m",
            "info": "\033[38;5;51m",
            "warn": "\033[38;5;220m",
            "ok": "\033[38;5;78m",
            "error": "\033[38;5;203m",
        }
        prefix = {"section": "◆", "info": "·", "warn": "!", "ok": "✓", "error": "✗"}[kind]
        print(f"{colors[kind]}{prefix} {text}\033[0m", file=sys.stderr)
    else:
        print(text, file=sys.stderr)


def _cli_gate(home, timeout_seconds: int = 900,
              conversation_id: str = "") -> ApprovalGate:
    from ..interface.autonomy import policy_for

    def decide(kind: str, title: str, body: str) -> Decision:
        print(f"\n[{kind}] {title}\n{body}\n")
        answer = input("Approve? [y/N] ").strip().lower()
        return Decision("approved" if answer in ("y", "yes") else "rejected")

    gate = ApprovalGate(
        decide,
        timeout_seconds=timeout_seconds,
        policy=policy_for(home),
    )
    # Pre-approval applies to CLI and scheduled runs too, not only to
    # the WebUI: the store is the same file the WebUI, WhatsApp, and
    # Slack surfaces write, keyed by the run's conversation id.
    try:
        from ..interface.preapproval import PreapprovalStore
        from ..interface.preapproval_gate import PreapprovalGate
        return PreapprovalGate(gate, PreapprovalStore(home), conversation_id)
    except Exception:
        return gate


def _scheduled_gate(sandboxed: bool = False) -> ApprovalGate:
    """Non-blocking gate for unattended scheduled runs.

    A daemon tick loop must never block on stdin: _cli_gate's
    input() either hangs the loop or dies on EOF in a headless
    daemon. This gate decides immediately, with no policy
    auto-approval and no prompt: email tools are approved (the
    email tool itself enforces owner-only delivery for scheduled
    runs, so the gate cannot leak mail to other recipients),
    sandboxed shell commands that pass the practice safety scan
    are approved, and everything else is rejected fail-closed
    and fast. A scheduled goal that needs install, publish, or
    live approval therefore fails with a clear rejection
    instead of hanging the tick loop — it never worked
    unattended before either, it just failed slowly.
    """
    def decide(kind: str, title: str, body: str) -> Decision:
        if kind == "live" and title in ("Extra tool email_send",
                                        "Extra tool email_make_draft"):
            return Decision("approved")
        if kind == "mandatory" and sandboxed:
            from ..interface.practice import _mandatory_safe_for_practice
            if _mandatory_safe_for_practice(body):
                return Decision("approved",
                                reason="scheduled shell command")
        return Decision("rejected")

    return ApprovalGate(decide, policy=None)


def _make_web_researcher(config, conversation_context: str = ""):
    """Search researcher shared by interactive and scheduled runs.

    Include result URLs so interview recommendations can cite the
    sources behind current claims instead of presenting snippets alone.
    """
    def _researcher(goal_text):
        try:
            from ..agentic.research_allow import DomainGrants, load_allowlist
            from ..capabilities.search import search as _s
            search_text = goal_text
            if conversation_context:
                search_text = (f"Conversation topic:\n{conversation_context}\n\n"
                               f"Current request:\n{goal_text}")
            _r = _s(search_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                    allowlist=load_allowlist(config.home, config.research_allowlist),
                    grants=DomainGrants(config.home))
            return [f"{x.get('title', '')}: {x.get('snippet', '')} ({x.get('url', '')})"
                    for x in _r.get("results", []) if x.get("url")]
        except Exception:
            return []
    return _researcher


def _network_available(execution_backend, sandbox) -> bool:
    """True when shell steps can reach the network, for the loop's
    network-on announcement and the sandbox offline hint. A Docker
    backend reports its own flag; a bubblewrap backend is always
    offline; with no backend, shell runs through Sandbox, which is
    offline when bwrap is present and on the host network otherwise."""
    if execution_backend is not None:
        return bool(getattr(execution_backend, "allow_network", False))
    return False if getattr(sandbox, "bwrap", None) else True


def _run_workspace(config: Config, run_id: str) -> Path:
    """Create one timestamped workspace directory for a run."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    path = config.workdir / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def _run_sandbox(config: Config, run_id: str) -> Sandbox:
    return Sandbox(
        _run_workspace(config, run_id), config.sandbox_timeout_seconds,
        mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
        fsize_mb=config.sandbox_fsize_mb,
        cpu_seconds=config.sandbox_cpu_seconds,
        backend=config.sandbox_backend,
        allow_network=config.sandbox_network,
        require_isolation=config.sandbox_require_isolation)





def make_deep_researcher(config):
    """Loop-side deep researcher: (query, chat) -> report dict.

    chat is the run's model callable (loop._chat with phase
    "research"), so the judge and synthesis run at the run's
    thinking level — max in research mode. Rounds/sources come
    from the deep-research env params.
    """
    from ..agentic.deep_research import deep_research as _deep
    search_fn, fetch_fn = _deep_search_fns(config)

    def deep_researcher(query, chat):
        return _deep(str(query), search_fn, fetch_fn, chat=chat,
                     max_rounds=_env_int("AIDON_DEEP_RESEARCH_ROUNDS", 2),
                     max_sources=_env_int("AIDON_DEEP_RESEARCH_SOURCES", 5))

    return deep_researcher


def _make_governor(config, budget_key=None):
    """One acquisition governor per run, from config caps.

    getattr defaults keep old configs working: a config
    without the acq_* fields still gets the starting policy.
    With a budget_key, counts and seconds resume from the
    mission's saved record instead of starting at zero.
    """
    from ..capabilities.acquisition_governor import (
        AcquisitionGovernor, load_budget_record)
    gov = AcquisitionGovernor(
        max_tools=getattr(config, "acq_max_tools", 2),
        max_skills=getattr(config, "acq_max_skills", 2),
        max_mcp=getattr(config, "acq_max_mcp", 1),
        max_dependency_sets=getattr(config, "acq_max_dependency_sets", 2),
        max_repair_cycles=getattr(config, "acq_max_repair_cycles", 3),
        max_acquisition_seconds=getattr(config, "acq_max_seconds", 600.0),
        home=getattr(config, "home", None),
        budget_key=budget_key)
    if budget_key:
        record = load_budget_record(getattr(config, "home", None), budget_key)
        if record:
            try:
                gov.tools = int(record.get("tools", 0))
                gov.skills = int(record.get("skills", 0))
                gov.mcp = int(record.get("mcp", 0))
                gov.dependency_sets = int(record.get("dependency_sets", 0))
                gov.repair_cycles = int(record.get("repair_cycles", 0))
                gov.acquisition_seconds = float(
                    record.get("acquisition_seconds", 0.0))
            except (TypeError, ValueError):
                pass
    return gov



def _make_memory(config: Config):
    """Memory with the real embedder if available, else Hash, with warning."""
    from ..memory.embedder import HashEmbedder, make_embedder_cached
    from ..memory.entity_graph import EntityGraph
    from ..memory.memory import Memory, SQLiteStore

    try:
        embedder = make_embedder_cached()
    except Exception as exc:
        print(f"warning: real embedder initialization failed "
              f"({type(exc).__name__}: {exc}); using HashEmbedder for this run",
              file=sys.stderr)
        embedder = HashEmbedder()

    entity_graph = EntityGraph(config.home)
    try:
        mem = Memory(config.home, embedder=embedder,
                     entity_graph=entity_graph)
        if type(mem.store).__name__ == "SQLiteStore":
            print("warning: Qdrant client not installed, using persistent "
                  "SQLite memory fallback (run ./setup.sh --with-qdrant "
                  "for a Qdrant server)", file=sys.stderr)
        return mem
    except Exception as exc:
        # A broken/unreachable Qdrant must not discard an already loaded
        # semantic embedder. SQLite keeps memory durable until Qdrant returns.
        print(f"warning: vector store initialization failed "
              f"({type(exc).__name__}: {exc}); using persistent SQLite "
              "memory fallback", file=sys.stderr)
        return Memory(config.home, embedder=embedder,
                      store=SQLiteStore(config.home),
                      entity_graph=entity_graph)


class _LazyMemory:
    """Build memory on first use, not at CLI start.

    Opening Qdrant and loading the 440M embedder costs seconds and
    hundreds of megabytes. Commands such as ``tools list``,
    ``schedule list``, and ``sandbox check`` never touch memory, so
    paying that cost before dispatch is pure startup latency. This
    proxy defers construction until a memory attribute is actually
    read.
    """

    def __init__(self, config: Config):
        self._config = config
        self._memory = None
        self._lock = threading.Lock()

    def _get(self):
        if self._memory is None:
            # The WebUI loads several panels concurrently.  Without a
            # lock, each first request can load the 440M embedder before
            # the others see the cached Memory instance.
            with self._lock:
                if self._memory is None:
                    self._memory = _make_memory(self._config)
        return self._memory

    def __getattr__(self, name):
        return getattr(self._get(), name)


