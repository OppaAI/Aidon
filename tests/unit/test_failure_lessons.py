"""Failure lessons: classification, recurrence merging,
repair linkage, shape-ranked retrieval, and the cap."""

import pytest

from src.memory.failure_lessons import (
    FailureLessons, classify_failure)


def _store(tmp_path, **kwargs):
    return FailureLessons(tmp_path, **kwargs)


def test_record_and_persist(tmp_path):
    store = _store(tmp_path)
    lesson = store.record_failure(
        "Build a paper search MCP server",
        "unknown tool: mcp_search",
        context={"tools": ["mcp_search"], "packages": ["mcp"]})
    assert lesson["failure_class"] == "capability"
    assert lesson["times_seen"] == 1
    assert lesson["repair"] is None
    fresh = _store(tmp_path)
    assert fresh.get(lesson["id"])["goal"] == lesson["goal"]


def test_recurrence_merges_same_shape_and_class(tmp_path):
    store = _store(tmp_path)
    first = store.record_failure(
        "Build a paper search MCP server", "unknown tool: mcp_search")
    second = store.record_failure(
        "Build paper search MCP server!", "unknown tool: mcp_other")
    assert first["id"] == second["id"]
    assert second["times_seen"] == 2
    assert len(store.list()) == 1
    assert second["failure_text"] == first["failure_text"], \
        "the first failure text is kept; the count tracks recurrence"


def test_different_class_is_a_separate_lesson(tmp_path):
    store = _store(tmp_path)
    a = store.record_failure("Build a paper search MCP server",
                             "unknown tool: mcp_search")
    b = store.record_failure("Build a paper search MCP server",
                             "timeout waiting for install")
    assert a["id"] != b["id"]
    assert len(store.list()) == 2


def test_classify_failure_rules():
    assert classify_failure("timeout after 120s") == "transient"
    assert classify_failure("Rate limit 429") == "transient"
    assert classify_failure("unknown tool: frobnicate") == "capability"
    assert classify_failure("unverified criterion: X exists") == "criterion"
    assert classify_failure("out.txt does not contain hello") == "verify"
    assert classify_failure("something odd happened") == "execution"


def test_record_repair_links_fix_and_tests(tmp_path):
    store = _store(tmp_path)
    lesson = store.record_failure("Build a paper search MCP server",
                                  "unknown tool: mcp_search")
    fixed = store.record_repair(
        lesson["id"], "built the MCP server plugin and adopted it",
        tests_added=["test_papers_search_returns_citations"])
    assert fixed["repair"]["description"].startswith("built the MCP")
    assert fixed["repair"]["tests_added"] == [
        "test_papers_search_returns_citations"]
    assert fixed["repair_count"] == 1
    with pytest.raises(ValueError, match="unknown lesson"):
        store.record_repair("nope", "did something")


def test_lessons_for_ranks_by_shape(tmp_path):
    store = _store(tmp_path)
    store.record_failure("Build a paper search MCP server",
                         "unknown tool: mcp_search")
    store.record_failure("Bake a strawberry tart",
                         "oven timeout")
    store.record_failure("Build a paper search MCP server",
                         "timeout waiting for install")
    hits = store.lessons_for("Build a paper search MCP server")
    assert [h["failure_class"] for h in hits] == ["capability", "transient"]
    assert hits[0]["match_score"] == 1.0
    assert store.lessons_for("Write a novel about Mars") == []


def test_cap_enforces_bound(tmp_path):
    store = _store(tmp_path, max_lessons=3)
    for i in range(5):
        store.record_failure(f"Goal number {i} alpha",
                             f"failure {i}")
    assert len(store.list()) == 3


def test_empty_goal_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="needs a goal"):
        store.record_failure("   ", "boom")
