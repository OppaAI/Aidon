"""Multi-agent research, model summary, redaction, pre-approval gate."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.approval import Decision
from src.model_summary import rolling_summary_model
from src.preapproval_gate import PreapprovalGate
from src.redaction import redact_dict, redact_text
from src.research_agents import run_research_agents


def test_research_agents_merge_dedup_and_one_angle_failure():
    def researcher(query, angle):
        assert query == "worm game"
        if angle == "details":
            raise RuntimeError("researcher down")
        if angle == "overview":
            return ["Worms eat apples", "Worms grow"]
        return ["Worms grow", "Worms avoid walls"]

    result = run_research_agents("worm game", researcher)
    assert result["query"] == "worm game"
    assert result["angles"] == ["overview", "details", "counterpoints"]
    assert result["facts"] == ["Worms eat apples", "Worms grow", "Worms avoid walls"]
    assert result["failed_angles"] == ["details"]


def test_research_agents_default_angles_and_empty_query():
    seen = []

    def researcher(query, angle):
        seen.append(angle)
        return [f"fact-{angle}"]

    result = run_research_agents("q", researcher, angles=["a", "b"])
    assert result["angles"] == ["a", "b"]
    assert result["facts"] == ["fact-a", "fact-b"]
    try:
        run_research_agents("", researcher)
        assert False, "empty query must raise"
    except ValueError:
        pass


def test_model_summary_uses_chat_phase_and_falls_back():
    class PhaseClient:
        def __init__(self):
            self.calls = []

        def chat_phase(self, phase, messages):
            self.calls.append((phase, messages))
            return "Model summary: worms grow."

        def chat(self, messages):
            raise AssertionError("chat_phase should be preferred")

    client = PhaseClient()
    result = rolling_summary_model("Old summary", ["Worm ate apple"], client)
    assert result == "Model summary: worms grow."
    assert client.calls[0][0] == "summarize"
    assert "Worm ate apple" in client.calls[0][1][0].content
    assert "Old summary" in client.calls[0][1][0].content


def test_model_summary_falls_back_on_failure_and_empty():
    class FailingClient:
        def chat(self, messages):
            raise RuntimeError("model down")

    result = rolling_summary_model("Old", ["Event one", "Event two"], FailingClient())
    assert result == "Old | Event one; Event two"

    class EmptyClient:
        def chat(self, messages):
            return "   "

    assert rolling_summary_model("Old", ["Event one"], EmptyClient()) == "Old | Event one"
    assert rolling_summary_model("", ["Only event"], None) == "Only event"


def test_redaction_removes_credentials_keeps_ordinary_text_and_emails():
    text = (
        "Call with Bearer abcdefgh12345678 and key sk-abcdefghijklmnop1234, "
        "AIDON_API_KEY=secret-value-here, password=hunter2, "
        "contact billing@example.com, file notes.txt, price 42, token count is 5"
    )
    redacted = redact_text(text)
    assert "abcdefgh12345678" not in redacted
    assert "sk-abcdefghijklmnop1234" not in redacted
    assert "secret-value-here" not in redacted
    assert "hunter2" not in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "AIDON_API_KEY=[REDACTED]" in redacted
    # Emails, file paths, ordinary numbers, and bare words are NOT redacted here.
    assert "billing@example.com" in redacted
    assert "notes.txt" in redacted
    assert "42" in redacted
    assert "token count is 5" in redacted


def test_redaction_dict_recursive():
    data = {"note": "Bearer abcdefgh12345678", "nested": ["plain", {"key": "sk-abcdefghijklmnop1234"}], "count": 3}
    redacted = redact_dict(data)
    assert redacted["note"] == "Bearer [REDACTED]"
    assert redacted["nested"][1]["key"] == "[REDACTED]"
    assert redacted["nested"][0] == "plain"
    assert redacted["count"] == 3


class _FakeGate:
    def __init__(self):
        self.asked = []

    def ask(self, kind, title, body):
        self.asked.append(kind)
        return Decision("rejected")


class _FakeStore:
    IRREVERSIBLE = {"delete", "live_execute", "publish", "spend", "send_external"}

    def __init__(self, approved_kinds):
        self.approved_kinds = set(approved_kinds)

    def is_preapproved(self, spec_id, kind, now):
        assert spec_id == "spec-1"
        assert isinstance(now, float)
        return kind in self.approved_kinds


def test_preapproval_gate_approves_reversible_preapproved():
    gate = _FakeGate()
    store = _FakeStore({"notify", "draft"})
    wrapped = PreapprovalGate(gate, store, "spec-1")
    assert wrapped.ask("notify", "Notify", "body").verdict == "approved"
    assert gate.asked == [], "pre-approved reversible kind must not ask the real gate"


def test_preapproval_gate_asks_when_not_preapproved():
    gate = _FakeGate()
    store = _FakeStore(set())
    wrapped = PreapprovalGate(gate, store, "spec-1")
    assert wrapped.ask("notify", "Notify", "body").verdict == "rejected"
    assert gate.asked == ["notify"]


def test_preapproval_gate_never_auto_approves_irreversible_or_external():
    gate = _FakeGate()
    # Even if a fake store claims these are approved, the gate must still ask.
    store = _FakeStore({"delete", "live_execute", "send_external", "external_email"})
    wrapped = PreapprovalGate(gate, store, "spec-1")
    for kind in ("delete", "live_execute", "send_external", "external_email", "publish", "spend"):
        assert wrapped.ask(kind, "Title", "body").verdict == "rejected"
    assert gate.asked == ["delete", "live_execute", "send_external", "external_email", "publish", "spend"]
