from types import SimpleNamespace

from src.agentic.validator import validate_plan


def test_plan_rejects_renamed_explicit_filename():
    plan = SimpleNamespace(
        steps=[{"tool": "write", "args": {"file": "dispatcher-webui-check.txt"},
                "deliverable": "dispatcher-webui-check.txt"}],
        deliverables=["dispatcher-webui-check.txt"])

    failures = validate_plan(
        plan,
        goal=("Create exactly one file named "
              "dispatcher-webui-check-20261101.txt in the workspace."))

    assert any("dispatcher-webui-check-20261101.txt" in failure
               for failure in failures)


def test_plan_accepts_exact_explicit_filename():
    requested = "dispatcher-webui-check-20261101.txt"
    plan = SimpleNamespace(
        steps=[{"tool": "write", "args": {"file": requested},
                "deliverable": requested}],
        deliverables=[requested])

    assert validate_plan(
        plan, goal=f"Create exactly one file named {requested}.") == []


def test_unmentioned_filenames_remain_allowed():
    plan = SimpleNamespace(
        steps=[{"tool": "write", "args": {"file": "result.txt"},
                "deliverable": "result.txt"}],
        deliverables=["result.txt"])

    assert validate_plan(plan, goal="Create the requested artifact.") == []
