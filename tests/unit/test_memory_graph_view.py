"""Memory graph page: /graph serves graph.html, /api/memory/graph
returns point + entity nodes with point->entity edges."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer
from src.memory.embedder import HashEmbedder
from src.memory.entity_graph import EntityGraph
from src.memory.memory import InMemoryStore, Memory


def _server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    memory = Memory(home, embedder=HashEmbedder(), store=InMemoryStore(),
                    entity_graph=EntityGraph(home))
    memory.record("episodic", "Fresh bread from Northside Bakery")
    memory.record("episodic", "Northside Bakery opens at dawn")
    memory.record("knowledge", "Northside Bakery sourdough starter guide")
    memory.record("episodic", "Unrelated gardening notes")
    events = []
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", events.append)
    server = WebUIServer(home, dispatcher=dispatcher, memory=memory)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return resp.status, resp.read().decode()


def test_graph_endpoint_nodes_and_edges(tmp_path):
    httpd, base = _server(tmp_path)
    try:
        status, body = _get(base, "/api/memory/graph")
        assert status == 200
        data = json.loads(body)
        point_ids = {n["id"] for n in data["nodes"] if n["type"] == "memory"}
        entity_ids = {n["id"] for n in data["nodes"] if n["type"] == "entity"}
        assert len(point_ids) == 4
        assert "entity:northside bakery" in entity_ids
        linked = {e["source"] for e in data["edges"]
                  if e["target"] == "entity:northside bakery"}
        assert len(linked) == 3
        # Collection filter narrows to that collection's points.
        _, body = _get(base, "/api/memory/graph?collection=knowledge")
        data = json.loads(body)
        assert {n["id"] for n in data["nodes"]
                if n["type"] == "memory"} != point_ids
        assert len([n for n in data["nodes"]
                    if n["type"] == "memory"]) == 1
    finally:
        httpd.shutdown()


def test_graph_page_served(tmp_path):
    httpd, base = _server(tmp_path)
    try:
        status, body = _get(base, "/graph")
        assert status == 200
        assert "/static/graph.js" in body
        status, body = _get(base, "/static/graph.js")
        assert status == 200
        assert "memory/graph" in body
    finally:
        httpd.shutdown()
