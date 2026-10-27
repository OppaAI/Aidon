"""Top category wiring: full runs, revoke, v1 to RunControl, phase router, 3D factory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.model_client import FakeClient, Message
from src.router import RoutedClient
from src.run_control import RunControl
from src.v1 import AuthStore, GoalStore, handle
from src.webui import FACTORY_HTML, WebUIGate
from src.scheduler_loop import AutonomyTracker, NudgeTracker


def test_auth_revoke():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        auth = AuthStore(Path(tmp))
        key, _record = auth.create_key("test", "owner")
        assert auth.verify(key) is not None
        assert auth.revoke("test") is True
        assert auth.verify(key) is None
        assert auth.revoke("test") is False


def test_v1_steer_cancel_write_run_control(tmp_path):
    auth = AuthStore(tmp_path)
    key, _record = auth.create_key("owner1", "owner")
    goals = GoalStore(tmp_path)
    goal = goals.submit("Write a note", "default", "owner1")
    headers = {"Authorization": key}
    status, payload = handle(auth, goals, "POST", f"/api/v1/goals/{goal.id}/steer", headers, {"note": "slower"})
    assert status == 200
    control = RunControl(tmp_path)
    assert "slower" in control.steer_notes(goal.id)
    status, _payload = handle(auth, goals, "POST", f"/api/v1/goals/{goal.id}/cancel", headers, {})
    assert status == 200
    assert control.is_cancelled(goal.id) is True


def test_routed_client_fast_and_strong_phases():
    fast = FakeClient("fast")
    strong = FakeClient("strong")
    client = RoutedClient(fast, strong, fast_phases={"interpret", "learn"})
    assert client.chat_phase("interpret", [Message("user", "hi")]) == "fast"
    assert client.chat_phase("plan", [Message("user", "hi")]) == "strong"
    assert client.chat_phase("verify", [Message("user", "hi")]) == "strong"
    assert client.stats() == {"fast_calls": 1, "strong_calls": 2}


def test_autonomy_never_auto_approves_live():
    tracker = AutonomyTracker(enabled=True, threshold=1)
    tracker.record_approval("low", 1000.0)
    assert tracker.should_auto_approve("low", 1001.0) is True
    assert tracker.should_auto_approve("high", 1001.0) is False
    tracker.record_approval("high", 1000.0)
    assert tracker.should_auto_approve("high", 1001.0) is False


def test_nudge_once_per_card():
    tracker = NudgeTracker(nudge_minutes=15, channel="C1")
    sent = []
    assert tracker.check("card-1", 0.0, 16 * 60, slack_sender=lambda ch, text: sent.append(text)) is True
    assert tracker.check("card-1", 0.0, 30 * 60, slack_sender=lambda ch, text: sent.append(text)) is False
    assert len(sent) == 1


def test_factory_is_3d_and_keeps_avatar_link():
    assert "three.min.js" in FACTORY_HTML
    assert "Test bench" in FACTORY_HTML
    assert "Foreman desk" in FACTORY_HTML
    assert "Warehouse" in FACTORY_HTML
    assert "/api/factory" in FACTORY_HTML
    from src.webui import CHAT_HTML
    assert 'id="avatar"' in CHAT_HTML
    assert "/factory" in CHAT_HTML
