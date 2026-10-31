"""Clef intent router: the same classify/mode_for shape as the
semantic router, decided by Clef instead of embeddings.

One Clef request answers two questions at once: the route
(chat / webchat / agentic / artifact) and the mode
(ask / plan / neither). The mode answer is cached per message
text so mode_for() costs no second call in the common path,
where run() classifies first and detects mode second.

Failure is fail-safe, never fatal: a transport error, an
unparseable answer, or an unknown key falls back to the
semantic router when one was given, else to the semantic
router's own safe default (agentic, 0.0). run() also wraps
classify in try/except, so a router failure can never break
a run — it just routes agentic.

Constructing a ClefRouter warms the endpoint in the
background (process-once, see ClefClient.ensure_warmed), so
the first routing decision after server start is not the
Modal cold start.
"""

from __future__ import annotations

from .clef_client import ClefClient

ROUTE_CRITERIA = {
    "chat": "Plain conversation: a greeting, a question answerable "
            "directly, or chat with no task, no file change, and no "
            "web research needed.",
    "webchat": "Needs current information from the web: news, facts, "
               "prices, or research from online sources.",
    "agentic": "A task to carry out with tools or files: make, change, "
               "find, compute, or do something.",
    "artifact": "Wants a built artifact: a document, page, app, image, "
                "or other deliverable file.",
}

MODE_CRITERIA = {
    "ask": "The user wants to be asked questions and work through "
           "details before anything is planned.",
    "plan": "The user wants a plan first, with no questions, or the "
            "task is large and should be planned before running.",
    "neither": "Neither: just do it, or no mode preference.",
}


class ClefRouter:
    def __init__(self, client: ClefClient, fallback=None):
        self.client = client
        self.fallback = fallback
        self._mode_cache: tuple[str, str | None] | None = None
        try:
            client.ensure_warmed()
        except Exception:
            pass

    def _ask_both(self, text: str):
        """One request for route + mode; returns (route, conf, mode)."""
        resp = self.client.decide(
            {"message": text},
            {"route": {"type": "choice",
                       "instructions": "Pick the best route for this message.",
                       "criteria": ROUTE_CRITERIA},
             "mode": {"type": "choice",
                      "instructions": "Pick the mode the user wants, if any.",
                      "criteria": MODE_CRITERIA}})
        answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
        route_ans = answers.get("route", {})
        mode_ans = answers.get("mode", {})
        route = route_ans.get("choice") if isinstance(route_ans, dict) else None
        if route not in ROUTE_CRITERIA:
            probs = route_ans.get("probabilities", {}) if isinstance(route_ans, dict) else {}
            route = max(probs, key=lambda k: probs[k]) if probs else None
        if route not in ROUTE_CRITERIA:
            raise RuntimeError("Clef: unparseable route answer")
        conf = route_ans.get("confidence")
        if not isinstance(conf, (int, float)):
            conf = (route_ans.get("probabilities", {}) or {}).get(route, 0.0)
        mode = mode_ans.get("choice") if isinstance(mode_ans, dict) else None
        if mode not in ("ask", "plan"):
            mode = None
        return route, float(conf or 0.0), mode

    def classify(self, text: str, judge=None) -> tuple[str, float]:
        """Route + confidence. judge is accepted and ignored: Clef
        IS the judge here; there is no second opinion to ask."""
        try:
            route, conf, mode = self._ask_both(text)
            self._mode_cache = (text, mode)
            return route, conf
        except Exception:
            if self.fallback is not None:
                try:
                    return self.fallback.classify(text)
                except TypeError:
                    return self.fallback.classify(text, judge)
                except Exception:
                    pass
            return "agentic", 0.0

    def mode_for(self, text: str) -> str | None:
        if self._mode_cache is not None and self._mode_cache[0] == text:
            return self._mode_cache[1]
        try:
            _route, _conf, mode = self._ask_both(text)
            self._mode_cache = (text, mode)
            return mode
        except Exception:
            if self.fallback is not None:
                try:
                    return self.fallback.mode_for(text)
                except Exception:
                    pass
            return None


def build_router(config, embedder, cache_path):
    """Router for interactive runs: Clef when configured, else semantic.

    Clef is selected only by AIDON_ROUTER=clef AND a configured
    AIDON_CLEF_URL; anything else keeps the semantic router, so
    an unconfigured checkout behaves exactly as before. Returns
    None only when even the semantic router cannot be built.
    """
    use_clef = (str(getattr(config, "intent_router", "") or "").strip().lower() == "clef"
                and bool(getattr(config, "clef_url", "") or ""))
    fallback = None
    try:
        from .semantic_router import SemanticRouter
        fallback = SemanticRouter(embedder, cache_path=cache_path)
    except Exception:
        fallback = None
    if use_clef:
        try:
            client = ClefClient(
                getattr(config, "clef_url", ""),
                model=getattr(config, "clef_model", "") or "Cloudflare/clef-flash",
                key=getattr(config, "clef_key", "") or "",
                secret=getattr(config, "clef_secret", "") or "")
            return ClefRouter(client, fallback=fallback)
        except Exception:
            return fallback
    return fallback
