"""Phase router, old Aidon ClawRouter idea, fast vs strong tier.

Old keys, only: AIDON_FAST_MODEL, AIDON_ROUTER_FAST_PHASES.
No new duplicate. When AIDON_FAST_MODEL names a
different model, cheap phases run on the fast tier and
hard phases run on the strong tier. Empty fast model
means everything runs on the strong model, exactly as
before, routing is opt-in.
Default fast phases, old Aidon: interpret, learn,
respond. Trace compaction always uses fast, in old
Aidon, because it is mechanical summarization.
This is different from the semantic router, which
classifies a goal by embedding, with no LLM call, into
chat, webchat, artifact, or agentic. The phase router
picks a model tier per phase, after the goal is already
in the pipeline. Both save tokens, in different places:
semantic router skips a full run for chat goals,
phase router spends fewer tokens on cheap phases.
If fast and strong are the same Spark model, the phase
router saves tokens only by shorter prompts for cheap
phases, not by a cheaper per-token price, and not by
a faster model. Real speed and cost saving needs a
genuinely smaller or cheaper fast model.
Stats: fast_calls, strong_calls, counted in code,
never by the model, so a report can show what the
router actually did.
"""

from __future__ import annotations

from ..system.model_client import Message, ModelClient

DEFAULT_FAST_PHASES = {"interpret", "learn", "respond"}


def parse_fast_phases(value: str) -> set[str]:
    if not value:
        return set(DEFAULT_FAST_PHASES)
    return {part.strip().lower() for part in value.split(",") if part.strip()}


class RoutedClient(ModelClient):
    def __init__(self, fast_client: ModelClient, strong_client: ModelClient,
                 fast_phases: set[str] | None = None):
        self.fast_client = fast_client
        self.strong_client = strong_client
        self.fast_phases = fast_phases if fast_phases is not None else set(DEFAULT_FAST_PHASES)
        self.fast_calls = 0
        self.strong_calls = 0

    def chat(self, messages: list[Message]) -> str:
        return self.chat_phase("", messages)

    def chat_phase(self, phase: str, messages: list[Message]) -> str:
        if phase.lower() in self.fast_phases:
            self.fast_calls += 1
            return self.fast_client.chat(messages)
        self.strong_calls += 1
        return self.strong_client.chat(messages)

    def stats(self) -> dict:
        return {"fast_calls": self.fast_calls, "strong_calls": self.strong_calls}
