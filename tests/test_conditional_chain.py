"""Conditional chain execution restored from old Aidon / AAA."""

from src.capabilities.chain import (
    eval_condition,
    render_template,
    resolve_path,
    run_chain,
    run_conditional_chain,
    validate_chain_spec,
    validate_conditional_chain,
)


def test_sequential_chain_still_works():
    """Original sequential path must keep working."""
    calls = []

    class Reg:
        def call(self, tool, args):
            calls.append((tool, args))
            return f"ok:{tool}"

    spec = {
        "steps": [
            {"tool": "write", "args": {"file": "a.txt", "text": "hi"},
             "deliverable": "a.txt"},
            {"tool": "read", "args": {"file": "a.txt"}, "deliverable": "read_a"},
        ]
    }
    assert validate_chain_spec(spec) == []
    result = run_chain(spec, Reg())
    assert result["ok"] is True
    assert len(result["executed"]) == 2
    assert calls[0][0] == "write"


def test_sequential_rejects_unknown_tool():
    spec = {"steps": [{"tool": "rm", "args": {}, "deliverable": "x"}]}
    failures = validate_chain_spec(spec)
    assert any("unknown tool" in f for f in failures)


def test_conditional_validation_ok():
    chain = [
        {"tool": "web_search", "args": {"query": "storm"}, "as": "wx"},
        {
            "if": {"field": "wx", "op": "contains", "value": "storm"},
            "then": [{"tool": "slack_send", "args": {"text": "Heads up: {wx}"}}],
            "else": [{"tool": "noop", "args": {}}],
        },
    ]
    assert validate_conditional_chain(chain) == []


def test_conditional_validation_rejects_bad_op():
    chain = [
        {
            "if": {"field": "x", "op": "regex", "value": "y"},
            "then": [],
        }
    ]
    errors = validate_conditional_chain(chain)
    assert any("if.op" in e for e in errors)


def test_eval_condition_contains():
    assert eval_condition(
        {"field": "wx", "op": "contains", "value": "storm"},
        {"wx": "big storm incoming"},
    )
    assert not eval_condition(
        {"field": "wx", "op": "contains", "value": "storm"},
        {"wx": "clear skies"},
    )


def test_eval_condition_unknown_field_is_false():
    assert not eval_condition(
        {"field": "missing", "op": "==", "value": 1},
        {"wx": "hi"},
    )


def test_resolve_path_nested_json_string():
    bindings = {"wx": '{"summary": "storm", "temp": 12}'}
    found, value = resolve_path("wx.summary", bindings)
    assert found and value == "storm"


def test_render_template():
    text = render_template("Heads up: {wx.summary}", {"wx": {"summary": "storm"}})
    assert text == "Heads up: storm"
    # Unknown path left intact
    text = render_template("x={missing}", {})
    assert text == "x={missing}"


def test_run_conditional_then_branch():
    calls = []

    def invoke(name, args):
        calls.append((name, args))
        if name == "web_search":
            return "big storm incoming"
        return "sent"

    chain = [
        {"tool": "web_search", "args": {"query": "aurora"}, "as": "wx"},
        {
            "if": {"field": "wx", "op": "contains", "value": "storm"},
            "then": [
                {
                    "tool": "slack_send",
                    "args": {"text": "Heads up: {wx}"},
                }
            ],
            "else": [{"tool": "noop", "args": {}}],
        },
    ]
    result = run_conditional_chain(chain, invoke)
    assert result["ok"] is True
    assert "web_search" in result["ran"]
    assert any("storm" in x for x in result["ran"])
    assert calls[-1][0] == "slack_send"
    assert "big storm incoming" in calls[-1][1]["text"]


def test_run_conditional_else_branch():
    calls = []

    def invoke(name, args):
        calls.append((name, args))
        if name == "web_search":
            return "clear skies"
        return "ok"

    chain = [
        {"tool": "web_search", "args": {"query": "weather"}, "as": "wx"},
        {
            "if": {"field": "wx", "op": "contains", "value": "storm"},
            "then": [{"tool": "slack_send", "args": {"text": "storm!"}}],
            "else": [{"tool": "log", "args": {"msg": "quiet"}}],
        },
    ]
    result = run_conditional_chain(chain, invoke)
    assert result["ok"] is True
    assert calls[-1][0] == "log"
    assert any("storm" in x for x in result["skipped"])


def test_one_bad_tool_does_not_abort_chain():
    def invoke(name, args):
        if name == "boom":
            raise RuntimeError("explode")
        return "ok"

    chain = [
        {"tool": "ok_tool", "args": {}, "as": "a"},
        {"tool": "boom", "args": {}},
        {"tool": "after", "args": {}},
    ]
    result = run_conditional_chain(chain, invoke)
    assert result["ok"] is False
    assert "ok_tool" in result["ran"]
    assert "after" in result["ran"]
    assert any("boom" in e for e in result["errors"])
Grok finished responding
