"""Coverage for computer use, chains, outbox, CLI commands, and WebUI tabs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.capabilities.computer_use import capture_screenshot, run_computer_use, validate_action
from src.capabilities.chain import load_chain_spec, run_chain, validate_chain_spec
from src.capabilities.outbox import Outbox
from src.interface.webui import CHAT_HTML


def test_computer_use_validation_and_approval():
    assert validate_action({"kind": "click", "x": 10, "y": 20}) == []
    assert validate_action({"kind": "click", "x": -1, "y": 20}) != []
    assert validate_action({"kind": "unknown"}) != []
    # No driver, irreversible action with approval, must not fake a click
    result = run_computer_use([{"kind": "click", "x": 10, "y": 20}], approve=lambda k, d: True)
    assert not result["ok"] and "no computer-use driver" in result["error"]
    # Without approval, refused before driver
    result = run_computer_use([{"kind": "type", "text": "hello"}], approve=lambda k, d: False)
    assert not result["ok"] and "not approved" in result["error"]
    # Screenshot with injected capture, fake driver for test only
    def fake_capture(path):
        Path(path).write_bytes(b"fake-png")
        return True
    result = run_computer_use([{"kind": "screenshot", "capture": fake_capture}], screenshot_dir="/tmp")
    assert result["ok"]


def test_chain_validate_and_run():
    assert validate_chain_spec({"steps": [{"tool": "write", "deliverable": "a.txt"}]}) == []
    assert validate_chain_spec({"steps": [{"tool": "unknown", "deliverable": "a.txt"}]}) != []
    assert validate_chain_spec({"steps": []}) != []
    class FakeRegistry:
        def call(self, tool, args):
            return "ok"
    result = run_chain({"steps": [{"tool": "write", "deliverable": "a.txt", "args": {}}]}, FakeRegistry())
    assert result["ok"]


def test_outbox_drain_keeps_failed():
    import tempfile
    from datetime import datetime, timedelta, timezone
    with tempfile.TemporaryDirectory() as tmp:
        outbox = Outbox(Path(tmp))
        outbox.queue("a@example.com", "hi", "body")
        result = outbox.drain(lambda item: False)
        assert result["sent"] == 0 and result["remaining"] == 1
        # Cadence: a failed item is not due again for 60s, so the
        # retry must pass a time at or past the rescheduled attempt.
        result = outbox.drain(lambda item: True,
                              now=datetime.now(timezone.utc) + timedelta(seconds=61))
        assert result["sent"] == 1 and result["remaining"] == 0


def test_webui_tabs_and_cli_commands():
    assert "/capabilities" in CHAT_HTML and "/memory" in CHAT_HTML and "/knowledge" in CHAT_HTML and "/reports" in CHAT_HTML
    system_dir = Path(__file__).resolve().parents[2] / "src" / "system"
    main_text = (system_dir / "main.py").read_text()
    cli_text = main_text + (system_dir / "cli_inspect.py").read_text()
    for cmd in ['"memory"', '"skills"', '"tools"', '"dag"', '"mission"', '"outbox"', '"export"', '"schedule"', '"sandbox"', '"chain"', '"chat"', '"computer"']:
        assert cmd in cli_text, cmd
    roadmap = (Path(__file__).resolve().parents[2] / "docs" / "ROADMAP.md").read_text()
    assert "Screenshot" in roadmap and "Computer use" in roadmap
    # Fly brain is still last entry
    assert roadmap.rstrip().endswith("turning into a bug.")
