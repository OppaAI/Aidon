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
