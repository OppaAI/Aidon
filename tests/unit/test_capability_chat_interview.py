import json

from src.agentic.loop import GoalRunner


def _runner(answers, replies):
    runner = GoalRunner.__new__(GoalRunner)
    answers = iter(answers)
    replies = iter(replies)
    runner.question_ask = lambda question: next(answers)
    runner._chat = lambda phase, messages: json.dumps(next(replies))
    return runner


def test_rejection_never_starts_interview():
    runner = _runner(["reject"], [])
    outcome, message = runner._capability_interview("Edit a video", "need_plugin")
    assert outcome == "rejected"
    assert "can’t fulfill" in message


def test_cancel_during_interview_does_not_prepare_a_brief():
    runner = _runner(["proceed", "cancel"], [
        {"question": "What should the tool do?"},
    ])
    outcome, _ = runner._capability_interview("Edit a video", "need_plugin")
    assert outcome == "rejected"


def test_interview_requires_explicit_brief_approval():
    runner = _runner(["proceed", "I don't know", "reject"], [
        {"question": "Which editor do you prefer?"},
        {"brief": "Use an editor selected after comparing options."},
    ])
    outcome, _ = runner._capability_interview("Edit a video", "need_plugin")
    assert outcome == "rejected"


def test_approved_brief_is_returned_for_planning():
    runner = _runner(["yes", "MP4", "proceed"], [
        {"question": "What output format?"}, {"brief": "Export MP4."},
    ])
    assert runner._capability_interview("Edit a video", "need_plugin") == (
        "approved", "Export MP4.")


def test_interrupted_interview_replays_saved_answers(tmp_path):
    runner = _runner(["proceed", "MP4"], [
        {"question": "What format?"}, {"question": "What duration?"}])
    runner.home = tmp_path
    runner.conversation_id = "chat"
    import pytest
    with pytest.raises(StopIteration):
        runner._capability_interview("Edit a video", "need_plugin")
    resumed = _runner(["30 seconds", "proceed"], [{"brief": "MP4, 30 seconds."}])
    resumed.home = tmp_path
    resumed.conversation_id = "chat"
    assert resumed._capability_interview("Edit a video", "need_plugin") == (
        "approved", "MP4, 30 seconds.")
