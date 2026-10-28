import json
from datetime import datetime, timedelta, timezone

from src.agentic.autonomy_metrics import AutonomyMetrics


def _metrics(tmp_path):
    return AutonomyMetrics(tmp_path)


def test_record_and_count_matching_filters(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.record("run_started", run_id="r1")
    metrics.record("task_success", run_id="r1", detail="menu pdf done")
    metrics.record("task_success", run_id="r2", detail="grocery app done")
    metrics.record("task_failed", run_id="r1", detail="store run failed")
    metrics.record("custom_event", run_id="r1", detail="something else")
    assert metrics.count_matching("task_success") == 2
    assert metrics.count_matching("task_success", run_id="r1") == 1
    assert metrics.count_matching("task_success", run_id="r2") == 1
    assert metrics.count_matching("task_success", detail_contains="menu") == 1
    assert metrics.count_matching("task_success", detail_contains="missing") == 0
    assert metrics.count_matching("task_failed") == 1
    assert metrics.count_matching("custom_event") == 1
    assert (tmp_path / "autonomy_metrics.json").is_file()


def test_record_rejects_empty_event(tmp_path):
    metrics = _metrics(tmp_path)
    try:
        metrics.record("")
    except ValueError:
        pass
    else:
        raise AssertionError("empty event name should raise")
    assert metrics.count_matching("") == 0


def test_summary_math_and_zero_success_rate(tmp_path):
    metrics = _metrics(tmp_path)
    summary = metrics.summary()
    assert summary["window_days"] == 30
    assert summary["runs_started"] == 0
    assert summary["tasks_succeeded"] == 0
    assert summary["tasks_failed"] == 0
    assert summary["success_rate"] == 0.0
    assert summary["skill_reuse_success_rate"] == 0.0
    assert summary["skill_failure_rate"] == 0.0
    metrics.record("run_started", run_id="r1")
    metrics.record("task_success", run_id="r1")
    metrics.record("task_success", run_id="r1")
    metrics.record("task_failed", run_id="r1")
    metrics.record("clarifying_question", run_id="r1")
    metrics.record("retry", run_id="r1")
    metrics.record("skill_created")
    metrics.record("skill_promoted")
    metrics.record("skill_retired")
    metrics.record("tool_adopted")
    metrics.record("tool_reused")
    metrics.record("router_fast_call", tokens=100)
    metrics.record("router_fast_call", tokens=50)
    metrics.record("router_strong_call", tokens=1000)
    metrics.record("quarantine")
    metrics.record("learn_cycle")
    summary = metrics.summary()
    assert summary["runs_started"] == 1
    assert summary["tasks_succeeded"] == 2
    assert summary["tasks_failed"] == 1
    assert summary["success_rate"] == 2 / 3
    assert summary["clarifying_questions"] == 1
    assert summary["retries"] == 1
    assert summary["skills_created"] == 1
    assert summary["skills_promoted"] == 1
    assert summary["skills_retired"] == 1
    assert summary["tools_adopted"] == 1
    assert summary["tool_reuses"] == 1
    assert summary["router_fast_calls"] == 2
    assert summary["router_fast_tokens"] == 150
    assert summary["router_strong_calls"] == 1
    assert summary["router_strong_tokens"] == 1000
    assert summary["quarantines"] == 1
    assert summary["learn_cycles"] == 1


def test_human_intervention_only_with_human_decided(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.record("human_intervention", detail="auto flag, no decision")
    metrics.record("human_intervention", detail="owner approved", human_decided=True)
    metrics.record("human_intervention", detail="owner rejected", human_decided=True)
    assert metrics.count_matching("human_intervention") == 3
    assert metrics.summary()["human_interventions"] == 2


def test_skill_reuse_success_and_failure_rates(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.record("skill_reused", detail="success: reused menu skill")
    metrics.record("skill_reused", detail="success: reused budget skill")
    metrics.record("skill_reused", detail="failure: stale skill")
    metrics.record("skill_reused", detail="no outcome noted")
    summary = metrics.summary()
    assert summary["skill_reuses"] == 4
    assert summary["skill_reuse_success_rate"] == 2 / 4
    assert summary["skill_failure_rate"] == 1 / 4


def test_window_exclusion_via_direct_json_write(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.record("task_success", run_id="recent")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    path = tmp_path / "autonomy_metrics.json"
    data = json.loads(path.read_text())
    data["events"].append({
        "event": "task_success", "run_id": "old", "detail": "",
        "tokens": 0, "human_decided": False, "timestamp": old,
    })
    data["events"].append({
        "event": "task_failed", "run_id": "old", "detail": "",
        "tokens": 0, "human_decided": False, "timestamp": old,
    })
    path.write_text(json.dumps(data))
    summary = metrics.summary(days=30)
    assert summary["tasks_succeeded"] == 1
    assert summary["tasks_failed"] == 0
    assert summary["success_rate"] == 1.0
    # The old events are still in the log, just outside the window.
    assert metrics.count_matching("task_success") == 2


def test_render_contains_computed_numbers(tmp_path):
    metrics = _metrics(tmp_path)
    metrics.record("run_started")
    metrics.record("task_success")
    metrics.record("task_failed")
    metrics.record("router_fast_call", tokens=42)
    text = metrics.render()
    assert text.startswith("# Autonomy report")
    assert "- runs started: 1" in text
    assert "- tasks succeeded: 1" in text
    assert "- tasks failed: 1" in text
    assert "- success rate: 50.0%" in text
    assert "- router fast tokens: 42" in text
    assert "- human interventions: 0" in text


def test_persistence_across_instances(tmp_path):
    first = _metrics(tmp_path)
    first.record("run_started", run_id="r9")
    second = _metrics(tmp_path)
    assert second.count_matching("run_started", run_id="r9") == 1
    assert second.summary()["runs_started"] == 1
