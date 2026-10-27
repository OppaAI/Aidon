"""Avatar and mic in WebUI, DAG editor, entity graph for memory and KB."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agentic.dag_store import DagStore
from src.memory.embedder import HashEmbedder
from src.memory.entity_graph import EntityGraph, extract_entities
from src.memory.memory import Memory
from src.interface.triggers import TriggerDispatcher
from src.interface.webui import CHAT_HTML, DAG_HTML, WebUIServer


def test_chat_html_has_avatar_and_mic():
    assert 'id="avatar"' in CHAT_HTML
    assert 'id="mic"' in CHAT_HTML
    assert "SpeechRecognition" in CHAT_HTML
    assert "webkitSpeechRecognition" in CHAT_HTML
    assert "speechSynthesis" in CHAT_HTML
    assert "/dag" in CHAT_HTML


def test_dag_html_has_editor_controls():
    assert "DAG editor" in DAG_HTML
    assert "Add node" in DAG_HTML
    assert "Validate and save" in DAG_HTML
    assert "Move up" in DAG_HTML
    assert "Move down" in DAG_HTML


def test_dag_store_save_validate_and_list(tmp_path):
    store = DagStore(tmp_path)
    ok, failures = store.save("worm", "Write a Worm game", [
        {"id": "logic", "tool": "write", "args": {"file": "worm.py", "text": "Worm"}, "deliverable": "worm.py", "depends_on": []},
        {"id": "tests", "tool": "write", "args": {"file": "worm_test.py", "text": "Tests"}, "deliverable": "worm_test.py", "depends_on": ["logic"]}])
    assert ok is True and failures == []
    assert store.list_dags()[0]["id"] == "worm"
    assert store.get("worm")["nodes"][1]["id"] == "tests"
    # Invalid DAG, cycle, is not saved.
    ok, failures = store.save("bad", "Bad", [
        {"id": "a", "tool": "write", "args": {}, "deliverable": "a.py", "depends_on": ["b"]},
        {"id": "b", "tool": "write", "args": {}, "deliverable": "b.py", "depends_on": ["a"]}])
    assert ok is False and failures
    assert store.get("bad") is None


def test_webui_dag_api(tmp_path):
    home = tmp_path
    server = WebUIServer(home, dispatcher=TriggerDispatcher())
    status, payload = server.handle_dag_save({"id": "worm", "goal": "Write a Worm game", "nodes": [
        {"id": "logic", "tool": "write", "args": {"file": "worm.py"}, "deliverable": "worm.py", "depends_on": []}]})
    assert status == 200 and payload["ok"] is True
    assert server.handle_dags_list()["dags"][0]["id"] == "worm"
    assert server.handle_dag_delete("worm")["ok"] is True


def test_extract_entities():
    entities = extract_entities("Northside Bakery ordered Worm game, see worm.py and menu.pdf")
    assert "northside bakery" in entities
    assert "worm.py" in entities
    assert "menu.pdf" in entities


def test_entity_graph_related_and_memory_recall(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    graph = EntityGraph(home)
    graph.index_point("episodic", "p1", "Worm game logic, worm.py")
    graph.index_point("episodic", "p2", "Worm collision tests, worm_test.py")
    graph.index_point("knowledge", "k1", "Unrelated baking notes")
    related = graph.related("episodic", "p1")
    assert related and related[0]["point_id"] == "p2"
    assert graph.points_for("worm") or graph.points_for("worm.py")
    # Memory integration: KB and episodic points both enter the graph.
    memory = Memory(home, embedder=HashEmbedder(), entity_graph=graph)
    point = memory.record("knowledge", "Northside Bakery menu, menu.pdf")
    assert "northside bakery" in graph.entities_for_point("knowledge", point.id)


def test_webui_entities_api(tmp_path):
    home = tmp_path
    graph = EntityGraph(home)
    graph.index_point("episodic", "p1", "Worm game")
    memory = Memory(home, embedder=HashEmbedder(), entity_graph=graph)
    server = WebUIServer(home, dispatcher=TriggerDispatcher(), memory=memory)
    result = server.handle_entities("Worm")
    assert result["related"] == [["episodic", "p1"]]
