"""Clef decision client: typed questions in, probabilities out.

Clef is a decision model, not a chat model: it never generates
text. A request carries a state (anything JSON) and questions of
type choice, score, or noul, and the response carries one answer
per question with a choice or score, a confidence, and the full
probability distribution. Output tokens are always zero: one
forward pass, no autoregressive decoding, which is what makes it
cheap enough for routing and triage.

Transport is stdlib urllib only, matching the codebase's
HttpModelClient style. A custom transport callable may be
injected for tests; production always uses HTTP.

Cold start: a Modal container scales to zero after its
scaledown window, so the first request after idle waits for
boot plus weight load (minutes). ensure_warmed() fires one
background warmup per process so the first real decision is
not the cold one; it never blocks startup and never raises.
Runtime decide() calls use a bounded timeout and let the
caller fall back (semantic router, proceed) on failure —
a decision service must never hang a run.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request


def _default_transport(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Clef HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Clef request failed: {exc}") from exc


class ClefClient:
    def __init__(self, base_url: str, model: str = "Cloudflare/clef-flash",
                 key: str = "", secret: str = "", transport=None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._headers = {}
        if key and secret:
            # Modal proxy auth: send only when both parts are set.
            self._headers = {"Modal-Key": key, "Modal-Secret": secret}
        self._transport = transport or _default_transport
        self._warm_lock = threading.Lock()
        self._warm_thread = None

    def decide(self, state, questions: dict, images=None) -> dict:
        """POST one SystemOne request; returns the raw response dict."""
        payload = {"model": self.model, "state": state, "questions": questions}
        if images:
            payload["images"] = images
        return self._transport(
            self.base_url + "/v1/systemone", payload, self._headers)

    def decide_choice(self, state, name: str, instructions: str,
                      criteria: dict) -> tuple[str, float, dict]:
        """One choice question -> (winning key, confidence, probabilities).

        The winning key comes from the answer's "choice" field when
        present, else the highest-probability key. Confidence is the
        answer's confidence when present, else the winning
        probability, else 0.0. Raises on transport failure or an
        unparseable answer so the caller can fall back.
        """
        resp = self.decide(state, {name: {
            "type": "choice", "instructions": instructions,
            "criteria": criteria}})
        answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
        ans = answers.get(name)
        if not isinstance(ans, dict):
            raise RuntimeError(f"Clef: no answer for {name!r}")
        probs = ans.get("probabilities", {}) or {}
        choice = ans.get("choice")
        if choice not in criteria:
            choice = None
        if choice is None and probs:
            choice = max(probs, key=lambda k: probs[k])
        if choice not in criteria:
            raise RuntimeError(f"Clef: unparseable choice answer for {name!r}")
        confidence = ans.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = probs.get(choice, 0.0) if isinstance(probs, dict) else 0.0
        return choice, float(confidence), dict(probs)

    def warmup(self) -> dict:
        """One tiny decision call, just to bring the container up."""
        return self.decide(
            {"warmup": True},
            {"warmup": {"type": "choice",
                        "instructions": "Warmup ping, pick the first option.",
                        "criteria": {"ok": "Warmup ping",
                                     "ignore": "Ignore this ping"}}})

    def ensure_warmed(self):
        """Fire one background warmup per process; returns the thread.

        Idempotent: the second call in the same process returns the
        first call's thread (or None once it finished) and starts
        nothing new. Failures are swallowed to stderr only — a
        cold or unreachable decision service must never break
        startup; the first real call falls back instead.
        """
        with self._warm_lock:
            if self._warm_thread is not None:
                return self._warm_thread

            def _run():
                try:
                    self.warmup()
                except Exception as exc:
                    print(f"Clef warmup failed: {exc}", file=sys.stderr)
                    # Allow a later construction to retry: a failed
                    # warmup usually means the container was still
                    # booting, not that the service is gone.
                    with self._warm_lock:
                        if self._warm_thread is thread:
                            self._warm_thread = None

            thread = threading.Thread(target=_run, daemon=True,
                                      name="clef-warmup")
            self._warm_thread = thread
            thread.start()
            return thread
