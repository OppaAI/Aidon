"""Four gaps closed: demo ablation, autonomy policy, allowlist, caps."""

import json

from src.agentic.demo import (
    DemoScenario, InventToolScenario, RunResult,
    run_invent_tool_demo, run_learning_demo,
)
from src.agentic.research import Researcher
from src.agentic.research_agents import run_research_agents
from src.agentic.research_allow import (
    DomainGrants, StuckDetector, is_fetchable, research_multiround,
)
from src.capabilities.search import search
from src.interface.autonomy import (
    ACT, ACT_NOTIFY, ASK, AutonomyPolicy, policy_for,
)


# ── demo ablation ──
class _Skill:
    name = "worm-skill"
    version = "0.1.0"
    state = "validated"


def _run_task(task, skills):
    # With the skill: fewer tokens, skill used, success.
    # Without: more tokens, control fails in the failure->success case.
    if skills:
        return RunResult(success=True, tokens=80, tool_calls=2, skill_used=True)
    return RunResult(success=False, tokens=200, tool_calls=5, skill_used=False)


def test_demo_failure_to_success_is_demonstrated():
    scenario = DemoScenario(task_a="snake", task_b="worm",
                            make_skill=lambda: _Skill())
    report = run_learning_demo(scenario, _run_task, out=lambda *a: None)
    assert report["verdict"] == "demonstrated"
    assert "failure into a success" in report["reason"]


def test_demo_token_cut_gate():
    def run(task, skills):
        if skills:
            return RunResult(success=True, tokens=100, tool_calls=2, skill_used=True)
        return RunResult(success=True, tokens=200, tool_calls=3, skill_used=False)
    scenario = DemoScenario(task_a="a", task_b="b", make_skill=lambda: _Skill())
    report = run_learning_demo(scenario, run, out=lambda *a: None)
    assert report["verdict"] == "demonstrated"
    assert report["skilled"]["tokens"] == 100


def test_demo_no_skill_use_is_not_attributable():
    def run(task, skills):
        return RunResult(success=True, tokens=50, tool_calls=1, skill_used=False)
    scenario = DemoScenario(task_a="a", task_b="b", make_skill=lambda: _Skill())
    report = run_learning_demo(scenario, run, out=lambda *a: None)
    assert report["verdict"] == "not-demonstrated"
    assert "never used" in report["reason"]


def test_demo_no_skill_is_inconclusive():
    scenario = DemoScenario(task_a="a", task_b="b", make_skill=lambda: None)
    report = run_learning_demo(scenario, lambda t, s: RunResult(True),
                               out=lambda *a: None)
    assert report["verdict"] == "inconclusive"


def test_demo_invent_tool_gate():
    scenario = InventToolScenario(task_a="novel", task_b="reuse")
    report = run_invent_tool_demo(
        scenario,
        lambda t: {"success": True, "adopted": ["adder"]},
        lambda t, adopted: {"success": True, "reused": ["adder"]},
        out=lambda *a: None)
    assert report["verdict"] == "demonstrated"


# ── autonomy policy ──
def test_autonomy_graduates_medium_not_high(tmp_path):
    policy = AutonomyPolicy(path=tmp_path / "autonomy.json")
    assert policy.permission("skill")["action"] == ASK
    assert policy.permission("skill_privileged")["action"] == ASK
    assert policy.permission("unknown-class")["action"] == ASK
    policy.record_approval("skill")
    policy.record_approval("skill")
    assert policy.permission("skill")["action"] == ACT_NOTIFY
    for _ in range(3):
        policy.record_approval("skill")
    assert policy.permission("skill")["action"] == ACT_NOTIFY
    # High never graduates, however many approvals.
    for _ in range(6):
        policy.record_approval("skill_privileged")
    assert policy.permission("skill_privileged")["action"] == ASK


def test_autonomy_refusal_resets_and_forget_erases(tmp_path):
    policy = policy_for(tmp_path)
    policy.record_approval("skill")
    policy.record_approval("skill")
    policy.record_refusal("skill")
    assert policy.permission("skill")["action"] == ASK
    assert policy.approvals("skill") == 0
    policy.record_approval("skill")
    policy.forget("skill")
    assert policy.snapshot()["classes"] == {}
    # Persisted: a fresh policy on the same home sees the record.
    policy.record_approval("skill")
    again = policy_for(tmp_path)
    assert again.approvals("skill") == 1


def test_autonomy_decay(tmp_path):
    now = [1000.0]
    policy = AutonomyPolicy(decay_seconds=10, clock=lambda: now[0],
                            path=tmp_path / "a.json")
    policy.record_approval("skill")
    policy.record_approval("skill")
    now[0] = 2000.0
    assert policy.approvals("skill") == 0
    assert policy.permission("skill")["action"] == ASK


# ── allowlist wiring ──
def test_researcher_blocks_not_allowlisted():
    researcher = Researcher(allowlist=["example.com"])
    source = researcher.fetch("https://elsewhere.invalid/page")
    assert source.error.startswith("blocked")
    assert source.excerpt == ""


def test_researcher_grant_opens_one_host(tmp_path):
    grants = DomainGrants(tmp_path)
    grants.grant_run("elsewhere.invalid")
    researcher = Researcher(allowlist=["example.com"], grants=grants)
    assert is_fetchable("https://elsewhere.invalid/x", ["example.com"], grants)
    assert not is_fetchable("https://third.invalid/x", ["example.com"], grants)
    # Permanent grant survives a reload.
    grants.grant_permanent("perm.invalid")
    assert DomainGrants(tmp_path).is_granted("https://perm.invalid/x")
    assert (tmp_path / "domain_grants.json").is_file()


def test_search_filters_to_allowlist():
    def fake_post(url, payload, headers):
        return {"results": [
            {"title": "ok", "url": "https://example.com/a", "content": "x"},
            {"title": "no", "url": "https://elsewhere.invalid/b", "content": "y"},
        ]}
    out = search("q", backend="tavily", tavily_key="k", post=fake_post,
                 allowlist=["example.com"])
    assert [r["url"] for r in out["results"]] == ["https://example.com/a"]
    assert out["skipped_not_allowlisted"] == 1


def test_stuck_detector_and_multiround_cap():
    detector = StuckDetector(max_stuck_iterations=3)
    assert detector.see("a") is False
    assert detector.see("a") is False
    assert detector.see("a") is True
    calls = []
    def researcher(query):
        calls.append(query)
        # Facts on a host outside the allowlist: no allowed facts,
        # same signature every round, so the stuck break fires.
        return {"facts": [{"url": "https://elsewhere.invalid", "text": "same"}]}
    out = research_multiround("q", researcher, rounds=10,
                              allowlist=["example.com"],
                              max_stuck_iterations=2)
    assert len(calls) == 2
    assert out["facts"][0]["text"] == "same"


def test_research_agents_parallel_cap_is_passed_through():
    # Cap does not change results, only the pool size; smoke it.
    out = run_research_agents("q", lambda q, a: [f"{a}-fact"], max_workers=1)
    assert out["facts"] == ["overview-fact", "details-fact", "counterpoints-fact"]


def test_config_research_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_MAX_ROUNDS", "7")
    monkeypatch.setenv("AIDON_MAX_STUCK_ITERATIONS", "3")
    monkeypatch.setenv("AIDON_PARALLEL_TOOLS", "2")
    monkeypatch.setenv("AIDON_RESEARCH_ALLOWLIST", "example.com")
    monkeypatch.setenv("AIDON_HOME", str(tmp_path))
    from src.system.config import Config
    cfg = Config.load(home=tmp_path)
    assert cfg.research_max_rounds == 7
    assert cfg.max_stuck_iterations == 3
    assert cfg.parallel_tools == 2
    assert cfg.research_allowlist == "example.com"
