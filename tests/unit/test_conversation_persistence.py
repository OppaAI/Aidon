"""WebUI conversation rows and their transcript must survive refreshes."""

from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer
from src.memory.conversations_db import ConversationStoreDB


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
