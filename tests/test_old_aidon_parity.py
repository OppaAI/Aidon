"""Parity modules: each module is imported and exercised without fake live claims."""

import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.conversations import ConversationStore
from src.agentic.project_brief import save_brief, load_brief, validate_brief
from src.capabilities.docgen import generate_document, DocStore, build_ebook
from src.capabilities.mail_rules import apply_rules
from src.agentic.patterns import match_pattern
from src.agentic.prompts import render_prompt
from src.memory.recall_budget import RecallBudget
from src.capabilities.registry import registry_to_markdown
from src.capabilities.remote_hub import LocalHub
from src.interface.repl import Repl
from src.interface.report import autonomy_report
from src.capabilities.security_probe import probe_plugin
from src.capabilities.tool_discovery import list_tools, search_tools
from src.capabilities.uploads import UploadStore
from src.capabilities.audio import vrm_lip_sync_js
from src.interface.apikeys import ApiKeyStore
from src.agentic.intake import intake_from_message, validate_intake
from src.agentic.lifecycle import LifecycleStore
from src.agentic.planner import plan_goal
from src.agentic.eval import eval_run
from src.agentic.governor import Governor
from src.interface.slack_inbound import verify_slack_signature, parse_slack_event
from src.interface.whatsapp import parse_whatsapp_message, send_whatsapp_message
from src.interface.messaging import MessagingConfig

def test_conversations_and_brief(tmp_path):
    store = ConversationStore(tmp_path)
    store.record("c1", "user", "hello")
    assert store.history("c1")[0]["text"] == "hello"
    assert "hello" in store.context("c1")
    assert store.forget("c1")
    brief = {"goal": "dinner", "deliverables": ["menu"], "criteria": ["menu exists"]}
    assert validate_brief(brief) == []
    result = save_brief(tmp_path, "p1", brief)
    assert result["ok"] and load_brief(tmp_path, "p1")["goal"] == "dinner"

def test_docgen_mail_patterns_prompts(tmp_path):
    doc = generate_document("T", [{"heading": "H", "body": "B"}])
    assert "# T" in doc and "## H" in doc
    store = DocStore(tmp_path)
    store.save("doc1", doc)
    assert "doc1.md" in store.list()
    assert build_ebook("E", [{"title": "C1", "body": "B"}]).startswith("# E")
    assert apply_rules({"from": "a@b", "subject": "hi", "body": ""}, [{"name": "r", "from_contains": "a@b", "action": "draft"}])["action"] == "draft"
    assert apply_rules({"from": "x", "subject": "hi", "body": ""}, [{"name": "r", "from_contains": "a@b", "action": "draft"}])["action"] == "ignore"
    assert match_pattern("write a file")["name"] == "write_file"
    assert render_prompt("interpret", {"goal": "dinner"}) == "Interpret this goal, in one paragraph, with no extra tasks: dinner"

def test_recall_registry_hub_repl_report(tmp_path):
    budget = RecallBudget(max_points=1)
    assert not budget.check([{"text": "a"}, {"text": "b"}], ["episodic"])["ok"]
    assert budget.trim([1, 2]) == [1]
    md = registry_to_markdown({"sandbox_tools": ["write"], "plugin_tools": [], "skills": {}})
    assert "# Aidon registry" in md
    hub = LocalHub(tmp_path)
    src = tmp_path / "plugin1"; src.mkdir(); (src / "tool.py").write_text("x=1")
    hub.publish("plugin1", src)
    assert "plugin1" in hub.list()
    assert hub.fetch("plugin1", tmp_path / "out").exists()
    outputs = []
    repl = Repl(lambda goal: f"got {goal}", input_fn=iter(["/help", "hello", "/quit"]).__next__, output_fn=outputs.append)
    assert repl.run() == ["hello"]
    assert autonomy_report(tmp_path)["run_count"] == 0
    assert "AnalyserNode" in vrm_lip_sync_js() and "Fcl_MTH" in vrm_lip_sync_js() and "Fcl_EYE_Close" in vrm_lip_sync_js()

def test_security_tools_uploads_apikeys(tmp_path):
    plugin = tmp_path / "plug"; plugin.mkdir(); (plugin / "tool.py").write_text("import requests\n")
    findings = probe_plugin(plugin)
    assert any(f["type"] == "network" for f in findings)
    assert any(t["name"] == "write" for t in list_tools(tmp_path))
    assert search_tools(tmp_path, "writ")
    uploads = UploadStore(tmp_path)
    uploads.save("a.txt", b"hi")
    assert uploads.read("a.txt") == b"hi" and uploads.delete("a.txt")
    keys = ApiKeyStore(tmp_path)
    created = keys.create_key("test", "agent")
    assert keys.verify(created["key"])["name"] == "test"
    assert keys.list_keys()[0]["key_hash_prefix"] == created["key_hash_prefix"]
    assert keys.revoke("test")

def test_intake_lifecycle_planner_eval_governor():
    intake = intake_from_message("#dinner make a menu\nno milk")
    assert intake["project_id"] == "dinner" and validate_intake(intake) == []
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        life = LifecycleStore(Path(tmp))
        life.create("p1")
        assert life.transition("p1", "done")["ok"] is False
        assert life.transition("p1", "ready_for_review")["ok"] is True
        gov = Governor(Path(tmp), max_calls=1)
        assert gov.record("p1", calls=1)["ok"] is True
        assert gov.record("p1", calls=1)["ok"] is False
    plan = plan_goal("write a file")
    assert plan["valid"] and plan["pattern"] == "write_file"
    result = eval_run(["menu"], {"menu.md": "menu content"})
    assert result["ok"] and result["score"] == 1.0

def test_slack_whatsapp_inbound():
    assert parse_slack_event({"event": {"type": "message", "text": "hi", "channel": "C1"}}).goal == "hi"
    assert parse_slack_event({"event": {"type": "reaction_added"}}) is None
    payload = {"entry": [{"changes": [{"value": {"messages": [{"type": "text", "text": {"body": "hello"}, "from": "123"}]}}]}]}
    assert parse_whatsapp_message(payload).goal == "hello"
    cfg = MessagingConfig()
    assert send_whatsapp_message(cfg, "123", "hi")["ok"] is False
    # Slack signature with wrong secret must fail, not pass
    assert verify_slack_signature("secret", "123", b"body", "v0=bad") is False
