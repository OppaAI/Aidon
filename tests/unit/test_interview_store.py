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
    state = {"id": "chat", "created": True,
             "turns": [{"role": "user", "text": "Edit a video"}]}
    assert store.save_lab("tool", state, 0, "chat") == 1
    restored = InterviewStore(tmp_path).load_lab("tool", "chat")
    assert restored == {"revision": 1, "state": state}
    with pytest.raises(ValueError, match="another browser"):
        store.save_lab("tool", state, 0, "chat")
    assert store.load_lab("tool", "chat")["state"] == state
    with pytest.raises(ValueError, match="invalid lab"):
        store.load_lab("../../secret")


def test_lab_sessions_are_isolated_by_conversation(tmp_path):
    store = InterviewStore(tmp_path)
    first = {"id": "chat-a", "turns": [{"role": "user", "text": "first"}]}
    second = {"id": "chat-b", "turns": [{"role": "user", "text": "second"}]}
    assert store.save_lab("tool", first, 0, "chat-a") == 1
    assert store.save_lab("tool", second, 0, "chat-b") == 1
    assert store.load_lab("tool", "chat-a")["state"] == first
    assert store.load_lab("tool", "chat-b")["state"] == second
    assert store.load_lab("tool", "unknown") == {"revision": 0, "state": None}
    with pytest.raises(ValueError, match="matching conversation ID"):
        store.save_lab("tool", first, 1, "chat-b")


def test_legacy_lab_session_migrates_to_its_stable_conversation_id(tmp_path):
    import json
    import sqlite3
    path = tmp_path / "interviews.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE lab_sessions (kind TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL)")
    state = {"id": "legacy-chat", "created": True,
             "turns": [{"role": "user", "text": "saved"}]}
    db.execute("INSERT INTO lab_sessions VALUES (?,?,?)",
               ("tool", 3, json.dumps(state)))
    db.commit()
    db.close()

    store = InterviewStore(tmp_path)
    assert store.load_lab("tool", "legacy-chat") == {
        "revision": 3, "state": state}
    assert store.load_lab("tool", "another-chat") == {
        "revision": 0, "state": None}


def test_saved_lab_turns_restore_only_to_their_conversation(tmp_path):
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path)
    first = server.handle_conversation_create({})["id"]
    second = server.handle_conversation_create({})["id"]
    journal = InterviewStore(tmp_path)
    turn = journal.question("run", first, "Which format?", True)
    journal.answer(turn, "MP4")
    journal.question("run", first, "Ordinary question", False)
    assert server.handle_conversation_get(first)["lab_turns"] == [
        {"id": turn, "run_id": "run", "question": "Which format?", "answer": "MP4"}]
    assert server.handle_conversation_get(second)["lab_turns"] == []


def test_lab_conversation_id_is_stable_when_created_or_retried(tmp_path):
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path)
    conversation_id = "0123456789abcdef0123456789abcdef"
    first = server.handle_conversation_create({"id": conversation_id,
        "turns": [{"role": "user", "text": "saved once"}]})
    retry = server.handle_conversation_create({"id": conversation_id,
        "turns": [{"role": "user", "text": "must not be appended"}]})
    assert first["id"] == retry["id"] == conversation_id
    restored = server.handle_conversation_get(conversation_id)
    assert restored["id"] == conversation_id
    assert [turn["text"] for turn in restored["turns"]] == ["saved once"]


def test_reopened_conversation_offers_saved_interview_recovery(tmp_path):
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path)
    conversation = server.handle_conversation_create({})["id"]
    InterviewStore(tmp_path).save_session("unfinished", {
        "goal": "Edit the attached video", "kind": "need_plugin",
        "conversation_id": conversation, "finished": False,
        "outcome": "approved",
    })
    restored = server.handle_conversation_get(conversation)
    assert restored["recovery"] == {
        "goal": "Edit the attached video", "stage": "approved",
    }


def test_reopened_conversation_exposes_its_live_question(tmp_path):
    import threading
    import time
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path)
    conversation = server.handle_conversation_create({})["id"]
    other = server.handle_conversation_create({})["id"]
    server._web_runs["run"] = {"run_id": "run", "events": [], "trace": [], "status": "running"}
    thread = threading.Thread(target=server.ask_question,
                              args=("run", conversation, "[AIDON_LAB] Which format?"), daemon=True)
    thread.start()
    for _ in range(100):
        if server.waiting_question("run"):
            break
        time.sleep(0.01)
    restored = server.handle_conversation_get(conversation)
    assert restored["active_run"]["run_id"] == "run"
    assert restored["active_run"]["lab"] is True
    assert restored["lab_turns"][0]["question"] == "Which format?"
    assert server.handle_conversation_get(other)["active_run"] is None
    server.answer_question(conversation, "MP4")
    thread.join(timeout=2)
    assert not thread.is_alive()
