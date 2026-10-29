"""Scheduled runs: schedule validation, the scheduled gate, and
owner-only scheduled email.

Covers the RSS-prompt gap list: schedules that never fire are
rejected at creation instead of silently dead, scheduled runs
decide approvals without blocking on stdin, and their email tool
sends to the owner for real (Gmail keys present) while refusing
every other recipient.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.interface.triggers import Scheduler, SchedulerJob
from src.system.main import _goal_extra_tools, _scheduled_gate


def _add(scheduler, schedule, kind="goal", goal="do it", name="j"):
    scheduler.add_job(SchedulerJob(name=name, schedule=schedule,
                                   goal=goal, kind=kind))


def test_schedule_validation_accepts_firing_forms(tmp_path):
    scheduler = Scheduler(tmp_path / "home")
    _add(scheduler, "0 * * * *", name="a")
    _add(scheduler, "daily 08:00", name="b")
    _add(scheduler, "weekly Mon 08:00", name="c")
    _add(scheduler, "at:2099-01-01T08:00:00", name="d")
    assert len(scheduler.jobs) == 4


def test_schedule_validation_rejects_never_firing_forms(tmp_path):
    scheduler = Scheduler(tmp_path / "home")
    for bad in ("every 1 hours", "every 30 minutes",
                "2099-01-01T08:00:00", "not a schedule", "",
                "at:2000-01-01T08:00:00"):
        with pytest.raises(ValueError, match="never fires"):
            _add(scheduler, bad)
    assert scheduler.jobs == []


def test_scheduled_gate_approves_email_tools_only():
    gate = _scheduled_gate()
    assert gate.ask("live", "Extra tool email_send", "b").verdict == "approved"
    assert gate.ask("live", "Extra tool email_make_draft", "b").verdict == "approved"
    for kind, title in (("live", "Extra tool slack_send"),
                        ("live", "Approve live execution?"),
                        ("live", "Extra tool whatsapp_send"),
                        ("mandatory", "Run shell"),
                        ("install", "Install package"),
                        ("live_execute", "Approve desktop action")):
        assert gate.ask(kind, title, "b").verdict == "rejected"


def _tools(tmp_path, scheduled):
    config = SimpleNamespace(home=tmp_path, owner_email="owner@example.com")
    return _goal_extra_tools(config, tmp_path, scheduled=scheduled)


def _fake_gmail(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rtok")
    import src.capabilities.gmail as _gmail
    sent = []
    monkeypatch.setattr(_gmail, "gmail_sender",
                        lambda *a: lambda to, subject, body: (
                            sent.append((to, subject, body)),
                            {"id": "msg1"})[1])
    return sent


def test_scheduled_email_sends_to_owner_only(tmp_path, monkeypatch):
    sent = _fake_gmail(monkeypatch)
    tools = _tools(tmp_path, scheduled=True)
    owner_draft = tools["email_make_draft"]("owner@example.com", "Feed", "items")
    result = tools["email_send"](owner_draft)
    assert result.get("ok") is True and result.get("id") == "msg1"
    assert sent == [("owner@example.com", "Feed", "items\n")]
    other_draft = tools["email_make_draft"]("someone@else.com", "Feed", "items")
    refused = tools["email_send"](other_draft)
    assert refused.get("ok") is False
    assert len(sent) == 1


def test_scheduled_email_without_keys_keeps_draft(tmp_path, monkeypatch):
    for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    tools = _tools(tmp_path, scheduled=True)
    draft = tools["email_make_draft"]("owner@example.com", "Feed", "items")
    result = tools["email_send"](draft)
    assert result.get("ok") is False
    assert "no sender" in str(result.get("error", ""))


def test_interactive_email_now_sends_after_gate(tmp_path, monkeypatch):
    # Interactive runs pass the loop's send-gate (a human card)
    # before dispatch, so wiring the real sender there is safe —
    # previously email_send always failed with "no sender".
    sent = _fake_gmail(monkeypatch)
    tools = _tools(tmp_path, scheduled=False)
    draft = tools["email_make_draft"]("owner@example.com", "Feed", "items")
    result = tools["email_send"](draft)
    assert result.get("ok") is True
    assert len(sent) == 1
