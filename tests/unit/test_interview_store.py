import pytest

from src.interface.interview_store import InterviewStore


def test_transcript_survives_new_store_instance(tmp_path):
    store = InterviewStore(tmp_path)
    first = store.question("run", "conversation", "Which format?", True)
    store.answer(first, "MP4")
    store.question("run", "conversation", "Approve brief?", True)
    restored = InterviewStore(tmp_path).transcript("run")
    assert [(row["question"], row["answer"]) for row in restored] == [
        ("Which format?", "MP4"), ("Approve brief?", None)]
    assert all(row["conversation_id"] == "conversation" for row in restored)


def test_answer_cannot_be_overwritten(tmp_path):
    store = InterviewStore(tmp_path)
    turn = store.question("run", "conversation", "Approve?", True)
    store.answer(turn, "reject")
    with pytest.raises(ValueError):
        store.answer(turn, "proceed")
    assert InterviewStore(tmp_path).transcript("run")[0]["answer"] == "reject"


def test_identical_questions_do_not_replay_an_earlier_answer(tmp_path):
    store = InterviewStore(tmp_path)
    store.save_session("session", {"conversation_id": "chat", "finished": False,
        "pending": {"kind": "question", "prompt": "Approve?", "run_id": "run", "token": "first"}})
    first = store.question("run", "chat", "Approve?", False)
    store.answer(first, "yes")
    store.save_session("session", {"conversation_id": "chat", "finished": False,
        "pending": {"kind": "question", "prompt": "Approve?", "run_id": "run", "token": "second"}})
    assert store.answer_for_token("second", "chat") is None
    second = store.question("run", "chat", "Approve?", False)
    store.answer(second, "no")
    assert store.answer_for_token("first", "chat") == "yes"
    assert store.answer_for_token("second", "chat") == "no"
    assert store.answer_for_token("second", "other-chat") is None


def test_unfinished_interview_lookup_is_scoped_and_unambiguous(tmp_path):
    store = InterviewStore(tmp_path)
    store.save_session("one", {"goal": "edit video", "kind": "need_plugin", "conversation_id": "a", "finished": False})
    store.save_session("other", {"goal": "other", "kind": "need_mcp", "conversation_id": "b", "finished": False})
    assert store.unfinished_for_conversation("a")["goal"] == "edit video"
    assert store.unfinished_for_conversation("c") is None
    store.save_session("two", {"goal": "second", "conversation_id": "a", "finished": False})
    with pytest.raises(ValueError, match="Multiple"):
        store.unfinished_for_conversation("a")
    assert store.unfinished_for_conversation("a", goal="edit video")["goal"] == "edit video"
    assert store.unfinished_for_conversation("a", goal="new goal") is None


def test_standalone_lab_persists_and_rejects_stale_writes(tmp_path):
    store = InterviewStore(tmp_path)
    state = {"id": "chat", "turns": [{"role": "user", "text": "Edit a video"}]}
    assert store.save_lab("tool", state, 0) == 1
    restored = InterviewStore(tmp_path).load_lab("tool")
    assert restored == {"revision": 1, "state": state}
    with pytest.raises(ValueError, match="another browser"):
        store.save_lab("tool", {"turns": []}, 0)
    assert store.load_lab("tool")["state"] == state
    with pytest.raises(ValueError, match="invalid lab"):
        store.load_lab("../../secret")
