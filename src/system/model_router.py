"""Live model routing, beyond the fast-phase tier router.

The phase router in the agent layer only splits fast from strong.
A run also has phases that want a named model of their own, most
plainly verification, which should run on a verifier model when
one is configured and on the main model otherwise. RouterClient
is that map: a dict of phase -> client, plus a default client.

Rules, in order:
- A phase with its own entry uses that client.
- Any other phase uses the default client, never an error.
- A missing or empty phase name uses the default client.
- The client for a phase is looked up once per call, so a
  caller can replace a client in the map between calls.

chat() accepts both call shapes, because this router is used
both as a plain model client and as a phase-aware one:
``chat(messages)`` routes to the default client, and
``chat(phase, messages)`` routes to that phase's client.
chat_phase(phase, messages) is the explicit form.

client_for_routing(config, clients) builds the map from
configuration: clients is a dict of model name -> client, and
the map is derived from the config's main, fast, and verifier
model names. A config without a verifier field, or a clients
dict without that model, falls back to the default client, so
an older config or a test double never crashes routing.
"""

from __future__ import annotations

from ..system.model_client import ModelClient


def _fast_phases(config) -> set[str]:
    raw = getattr(config, "router_fast_phases", "") or ""
    if raw:
        phases = {part.strip().lower() for part in str(raw).split(",")
                  if part.strip()}
        if phases:
            return phases
    return {"interpret", "learn", "respond"}


def _model_client(clients: dict, model_name: str | None, default):
    if model_name and clients.get(model_name) is not None:
        return clients[model_name]
    return default


class RouterClient(ModelClient):
    """Dispatch a chat call to the client for its phase."""

    def __init__(self, clients: dict | None = None, default=None):
        self.clients = dict(clients or {})
        self.default = default
        self.calls_by_phase: dict[str, int] = {}

    def route(self, phase: str | None) -> object:
        """The client for a phase, or the default when unmapped."""
        if not phase:
            return self.default
        client = self.clients.get(str(phase).lower())
        return client if client is not None else self.default

    def chat(self, phase_or_messages, messages=None) -> str:
        """chat(messages) or chat(phase, messages), both supported."""
        if messages is None:
            return self.chat_phase("", phase_or_messages)
        return self.chat_phase(str(phase_or_messages), messages)

    def chat_phase(self, phase: str, messages, sampling=None) -> str:
        key = str(phase or "").lower()
        self.calls_by_phase[key] = self.calls_by_phase.get(key, 0) + 1
        client = self.route(phase)
        if client is None:
            raise ValueError("RouterClient has no default client")
        try:
            return client.chat(messages, sampling=sampling)
        except TypeError:
            return client.chat(messages)

    def stats(self) -> dict:
        return dict(self.calls_by_phase)


def client_for_routing(config, clients: dict) -> RouterClient:
    """Build a RouterClient from config model names and a model map.

    clients maps model name -> client, and may also carry a
    "default" entry. The default is the main model client, or
    the "default" entry, or the first client in the map, in
    that order. Fast phases map to the fast model client when
    the config names one and the map has it; the verify phase
    maps to the verifier model client on the same terms. A
    config attribute for the verifier model is read under
    either verifier_model or verifier, whichever is present.
    """
    clients = dict(clients or {})
    main_name = getattr(config, "model", "") or ""
    default = clients.get("default") or _model_client(clients, main_name, None)
    if default is None and clients:
        default = next(iter(clients.values()))
    phase_clients: dict[str, object] = {}
    fast_name = getattr(config, "fast_model", "") or ""
    if fast_name and fast_name != main_name:
        fast_client = _model_client(clients, fast_name, None)
        if fast_client is not None and fast_client is not default:
            for phase in _fast_phases(config):
                phase_clients[phase] = fast_client
    verifier_name = (getattr(config, "verifier_model", "") or ""
                     or getattr(config, "verifier", "") or "")
    if verifier_name and verifier_name != main_name:
        verifier_client = _model_client(clients, verifier_name, None)
        if verifier_client is not None and verifier_client is not default:
            phase_clients["verify"] = verifier_client
    # A phase-keyed entry in the map wins over a model-derived one:
    # a caller that already knows phases can pass them directly.
    for key, client in clients.items():
        if key in ("interpret", "learn", "respond", "verify", "plan",
                   "research", "sandbox", "approval", "live") and client is not None:
            phase_clients[key] = client
    return RouterClient(phase_clients, default=default)
