"""WebUI conversation rows and their transcript must survive refreshes."""

from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer
from src.memory.conversations_db import ConversationStoreDB
import threading


def test_start_goal_registers_a_browser_supplied_conversation_id(tmp_path):
    server = WebUIServer(tmp_path, dispatcher=TriggerDispatcher())
    conversation_id = "a" * 32

    result = server.start_goal({
        "goal": "remember this chat",
        "conversation_id": conversation_id,
    })

    assert result["conversation_id"] == conversation_id
    assert conversation_id in {
        item["id"] for item in server.handle_conversations_list()["conversations"]
    }
    restored = server.handle_conversation_get(conversation_id)
    assert restored["turns"][0]["text"] == "remember this chat"


def test_saved_chat_brief_and_run_guidance_reach_dispatcher(tmp_path):
    events = []
    ready = threading.Event()
    dispatcher = TriggerDispatcher()

    def capture(event):
        events.append(event)
        ready.set()

    dispatcher.register("webui", capture)
    server = WebUIServer(tmp_path, dispatcher=dispatcher)
    server.handle_brief_save(
        "chat-with-brief", {"brief": "Use concise answers and metric units."})

    result = server.start_goal({
        "goal": "How far is the walk?",
        "conversation_id": "chat-with-brief",
        "guidance": "Give the estimate first.",
    })

    assert result["conversation_id"] == "chat-with-brief"
    assert ready.wait(2)
    guidance = events[0].payload["guidance"]
    assert "Use concise answers and metric units." in guidance
    assert "Give the estimate first." in guidance


def test_store_recovers_orphaned_webui_exchange_rows(tmp_path):
    conversation_id = "b" * 32
    store = ConversationStoreDB(tmp_path)
    store.create_conversation(conversation_id)
    store.record_exchange(conversation_id, "user", "saved before restart")

    # Model the old failure mode: transcript rows exist but their parent
    # conversation row is absent.
    store._conn.execute("DELETE FROM conversations WHERE id = ?",
                        (conversation_id,))
    store._conn.commit()
    store._conn.close()

    restored = ConversationStoreDB(tmp_path)
    assert conversation_id in {
        item["id"] for item in restored.list_all_conversations()
    }
    assert restored.history(conversation_id)[0]["text"] == "saved before restart"
    restored._conn.close()
