"""Semantic router, from Aidon, embedding-based, no LLM call.

What it was used for in Aidon, exactly:
Classify a user goal, by embedding similarity, into
chat, webchat, artifact, or agentic, before the full
pipeline, so a chat question does not burn a full
plan, sandbox, verify, and approval run. Chat is
conversational, answerable directly, no tools or state
change. Webchat needs a web lookup, but produces no
artifact, so it goes on a fast lookup path, not the
full pipeline. Artifact must produce a file, so it
needs the plan and sandbox path, because the respond
path has no write tools. Agentic needs tools, planning,
file writes, research, or execution, so it runs the
full pipeline. When in doubt, it returns agentic, safe:
a needless pipeline costs time, answering directly
when action was needed is a missed task.
How it saves tokens: it replaces the interpret LLM
call for the routing decision, with one embedding
call, using the embedding model already loaded for
memory, and it skips the full pipeline for chat and
webchat goals. It does not make the model faster, it
avoids model calls entirely for the route, and avoids
a full run for chat goals.
Example phrases are the Aidon sets, copied as
data, not as code: chat greetings and questions,
webchat lookups, artifact file goals, agentic actions.
A router needs an embedder with embed_dense(list[str])
-> list[list[float]], in tests a fake embedder that
returns the example vectors, in production the real
EmbeddingGemma 2 embedder. Low confidence, below 0.15,
defaults to agentic, never to chat.
"""

from __future__ import annotations

import math

CHAT_EXAMPLES = [
    "hi", "hello", "hey there", "good morning", "good evening",
    "what can you do?", "what are your capabilities?", "who are you?",
    "tell me about yourself", "what's your name?", "how are you?",
    "thanks", "thank you", "bye", "goodbye",
    "what do you think about AI?", "tell me a joke",
    "what's the meaning of life?", "do you like music?",
]

WEBCHAT_EXAMPLES = [
    "can you check is it raining tomorrow?",
    "what's the weather in Vancouver?",
    "what are the best free TTS APIs available right now?",
    "who won the game last night?",
    "what's the latest news on AI?",
    "how much is Bitcoin right now?",
    "what's on TV tonight?",
    "is there traffic on the highway?",
    "what are the best espresso machines under 300 dollars?",
    "which laptop should I buy for programming?",
    "what are good restaurants near me?",
    "how much does a Steam Deck cost?",
    "what are the top rated books on fantasy this year?",
    "what's the difference between RAM and VRAM?",
    "is the new camera worth it?",
    "what's a good restaurant in Seattle for a business dinner?",
]

ARTIFACT_EXAMPLES = [
    "write a haiku about the ocean to haiku.md",
    "save a comparison of flask and django to cmp.md in markdown",
    "create a file called notes.txt with my shopping list",
    "write a python script that solves this and save it as solve.py",
    "put the meeting notes into summary.md",
    "export the data to results.csv",
    "write a 5-day sourdough starter schedule to sourdough.md",
    "generate a python script that renames my photos",
    "draft a cover letter and save it as cover.md",
    "make a checklist file for moving house",
    "store the research findings in a markdown document",
    "write down the recipe in a markdown file",
]

AGENTIC_EXAMPLES = [
    "run ls in my home directory",
    "research the top 3 Python web frameworks and save a comparison table",
    "find a sourdough tutorial on YouTube and summarize it",
    "search the web for the latest news on fusion energy",
    "summarize this PDF for me",
    "check my email for unread messages",
    "book a flight to Tokyo next week",
    "analyze this codebase and tell me what's wrong",
    "download that video and extract the audio",
    "research X and write the report to a file",
    "fix the failing test in the login module",
    "deploy the app to the staging server",
]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SemanticRouter:
    """Embedding-based chat / webchat / artifact / agentic classifier.

    Pre-made vectors? No, not in the repo, and not required.
    By default the router computes the example vectors on
    first construction, by embedding the 4 example lists,
    about 57 phrases, with the embedder already loaded for
    memory, and keeps them in memory for that router. That
    is cheap, but it repeats on every new router, and the
    WebUI handler builds a router per run, so it repeats
    per run. With cache_path, the first construction writes
    the vectors to that JSON file, pre-made from then on,
    and later constructions load them, no re-embedding,
    unless the embedder name or the example lists change,
    in which case the cache is ignored and recomputed.
    The cache file lives under the Aidon home, never in
    the repo, because vectors depend on the embedder model,
    and a HashEmbedder cache must never be reused for a
    real EmbeddingGemma 2 embedder, or vice versa.
    """

    def __init__(self, embedder, example_vectors: dict | None = None, cache_path=None):
        # Embedder interface, in order: embed_dense, embed_batch,
        # or single embed. HashEmbedder and EmbeddingGemma 2
        # use embed_batch, Aidon used embed_dense, so accept
        # all three, otherwise the router silently disables itself
        # in the WebUI handler's try/except, which is what was
        # happening before this fix, because HashEmbedder has no
        # embed_dense, and the real embedder has embed_batch.
        if hasattr(embedder, "embed_dense"):
            self._embed = embedder.embed_dense
        elif hasattr(embedder, "embed_batch"):
            self._embed = embedder.embed_batch
        else:
            self._embed = lambda texts: [embedder.embed(text) for text in texts]
        self._embedder_name = type(embedder).__name__
        self.cache_path = cache_path
        if example_vectors is not None:
            self._chat_vecs = example_vectors["chat"]
            self._webchat_vecs = example_vectors["webchat"]
            self._agentic_vecs = example_vectors["agentic"]
            self._artifact_vecs = example_vectors["artifact"]
            return
        cached = self._load_cache(cache_path) if cache_path else None
        if cached is not None:
            self._chat_vecs = cached["chat"]
            self._webchat_vecs = cached["webchat"]
            self._agentic_vecs = cached["agentic"]
            self._artifact_vecs = cached["artifact"]
            return
        self._chat_vecs = self._embed(CHAT_EXAMPLES)
        self._webchat_vecs = self._embed(WEBCHAT_EXAMPLES)
        self._agentic_vecs = self._embed(AGENTIC_EXAMPLES)
        self._artifact_vecs = self._embed(ARTIFACT_EXAMPLES)
        if cache_path:
            self._save_cache(cache_path)

    def _load_cache(self, cache_path):
        try:
            import json as _json
            from pathlib import Path as _Path
            data = _json.loads(_Path(cache_path).read_text())
            if data.get("embedder") != self._embedder_name:
                return None
            if data.get("examples") != {"chat": CHAT_EXAMPLES, "webchat": WEBCHAT_EXAMPLES,
                                        "agentic": AGENTIC_EXAMPLES, "artifact": ARTIFACT_EXAMPLES}:
                return None
            vecs = data["vectors"]
            if len(vecs["chat"]) != len(CHAT_EXAMPLES) or len(vecs["webchat"]) != len(WEBCHAT_EXAMPLES):
                return None
            return vecs
        except Exception:
            return None

    def _save_cache(self, cache_path):
        try:
            import json as _json
            from pathlib import Path as _Path
            path = _Path(cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"embedder": self._embedder_name,
                    "examples": {"chat": CHAT_EXAMPLES, "webchat": WEBCHAT_EXAMPLES,
                                 "agentic": AGENTIC_EXAMPLES, "artifact": ARTIFACT_EXAMPLES},
                    "vectors": {"chat": self._chat_vecs, "webchat": self._webchat_vecs,
                                "agentic": self._agentic_vecs, "artifact": self._artifact_vecs}}
            path.write_text(_json.dumps(data))
        except Exception:
            pass

    def classify(self, text: str) -> tuple[str, float]:
        vec = self._embed([text])[0]
        scores = [
            ("chat", max(_cosine(vec, v) for v in self._chat_vecs)),
            ("webchat", max(_cosine(vec, v) for v in self._webchat_vecs)),
            ("agentic", max(_cosine(vec, v) for v in self._agentic_vecs)),
            ("artifact", max(_cosine(vec, v) for v in self._artifact_vecs)),
        ]
        scores.sort(key=lambda x: x[1], reverse=True)
        best_cat, best_score = scores[0]
        second_score = scores[1][1]
        confidence = min(1.0, (best_score - second_score) * 5.0)
        if confidence < 0.15:
            return "agentic", confidence
        return best_cat, confidence
