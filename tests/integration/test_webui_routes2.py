"""WebUI routes, second batch: layout, voice, reports, missions,
capabilities, capability code, and memory episode/knowledge APIs.

Handlers are called directly on WebUIServer where that is
enough, and the HTTP routes are driven on an ephemeral
localhost port for the round-trips, as in test_webui_files.py.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from src.capabilities.mission import MissionStore
from src.capabilities.skill_library import Skill, SkillLibrary
from src.interface.webui import WebUIServer
from src.memory.memory import InMemoryStore, Point


def _server(tmp_path, memory=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return WebUIServer(home, memory=memory)


class _FakeMemory:
    """list_points + forget over an InMemoryStore, like the
    real Memory's surface for these two calls."""

    def __init__(self, store):
        self.store = store

    def list_points(self, collection, limit=50):
        return self.store.list_points(collection, limit=limit)

    def forget(self, collection, point_id):
        return self.store.delete(collection, point_id)


@pytest.fixture
def live_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    server = WebUIServer(home, memory=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, server, home
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _delete(base, path):
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ------------------------------------------------------------------- layout

def test_dag_layout_layers_along_a_chain(tmp_path):
    server = _server(tmp_path)
    status, payload = server.handle_dag_layout({"dag": {"nodes": [
        {"id": "a", "tool": "write", "depends_on": []},
        {"id": "b", "tool": "write", "depends_on": ["a"]},
        {"id": "c", "tool": "write", "depends_on": ["b"]},
        {"id": "d", "tool": "write", "depends_on": []},
    ]}})
    assert status == 200
    by_id = {entry["id"]: entry for entry in payload["layout"]}
    assert by_id["a"]["layer"] == 0 and by_id["a"]["x"] == 0
    assert by_id["b"]["layer"] == 1 and by_id["b"]["x"] == 200
    assert by_id["c"]["layer"] == 2 and by_id["c"]["x"] == 400
    assert by_id["d"]["layer"] == 0
    # y is the index within the layer, 90 apart.
    layer0 = [e for e in payload["layout"] if e["layer"] == 0]
    assert [e["y"] for e in layer0] == [0, 90]
    # Topological order: a dependency precedes its dependent.
    order = [e["id"] for e in payload["layout"]]
    assert order.index("a") < order.index("b") < order.index("c")


def test_dag_layout_rejects_invalid(tmp_path):
    server = _server(tmp_path)
    assert server.handle_dag_layout({})[0] == 400
    assert server.handle_dag_layout({"dag": {}})[0] == 400
    assert server.handle_dag_layout({"dag": {"nodes": [
        {"id": "", "tool": "write"}]}})[0] == 400
    # Unknown dependency.
    assert server.handle_dag_layout({"dag": {"nodes": [
        {"id": "a", "depends_on": ["ghost"]}]}})[0] == 400
    # Cycle.
    assert server.handle_dag_layout({"dag": {"nodes": [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]}]}})[0] == 400


def test_dag_layout_route_live(live_server):
    base, _, _ = live_server
    status, payload = _post(base, "/api/dags/layout", {"dag": {"nodes": [
        {"id": "a"}, {"id": "b", "depends_on": ["a"]}]}})
    assert status == 200
    assert payload["layout"][1]["layer"] == 1
    status, _ = _post(base, "/api/dags/layout", {"dag": {}})
    assert status == 400


# -------------------------------------------------------------------- voice

def test_voice_returns_booleans(tmp_path):
    server = _server(tmp_path)
    assert server.handle_voice() == {"announce": False, "tts_available": False}
    server.tts_key = "key-set"
    assert server.handle_voice()["tts_available"] is True
    server.voice_announce = True
    assert server.handle_voice()["announce"] is True


def test_voice_route_live(live_server):
    base, _, _ = live_server
    status, payload = _get(base, "/api/voice")
    assert status == 200
    assert payload == {"announce": False, "tts_available": False}


# ------------------------------------------------------------------ reports

def test_reports_list_and_get_round_trip(tmp_path):
    server = _server(tmp_path)
    assert server.handle_reports_list() == {"reports": []}
    server.handle_report_generate({"day": "2026-10-26", "summary": "did things"})
    assert server.handle_reports_list() == {"reports": ["2026-10-26"]}
    report = server.handle_report_get("2026-10-26")
    assert report["day"] == "2026-10-26"
    assert "did things" in report["content"]
    assert server.handle_report_get("no-such-day") is None


def test_reports_routes_live(live_server):
    base, server, _ = live_server
    server.handle_report_generate({"day": "2026-10-26", "summary": "live"})
    status, payload = _get(base, "/api/reports")
    assert status == 200 and payload == {"reports": ["2026-10-26"]}
    status, payload = _get(base, "/api/reports/2026-10-26")
    assert status == 200 and "live" in payload["content"]
    status, _ = _get(base, "/api/reports/no-such-day")
    assert status == 404
    # A traversal cannot escape the reports directory.
    status, _ = _get(base, "/api/reports/..%2F..%2Fsecret")
    assert status == 404


# ----------------------------------------------------------------- missions

def test_missions_list_empty_then_populated(tmp_path):
    server = _server(tmp_path)
    assert server.handle_missions_list() == {"missions": []}
    MissionStore(tmp_path / "home").create(
        "dinner", {"title": "Dinner", "deliverables": ["menu.pdf"]})
    missions = server.handle_missions_list()["missions"]
    assert len(missions) == 1
    assert missions[0]["conversation_id"] == "dinner"
    assert missions[0]["status"] == "draft"


def test_missions_route_live(live_server):
    base, server, home = live_server
    status, payload = _get(base, "/api/missions")
    assert status == 200 and payload == {"missions": []}
    MissionStore(home).create("job", {"title": "Job"})
    status, payload = _get(base, "/api/missions")
    assert status == 200 and len(payload["missions"]) == 1


# ------------------------------------------------------------- capabilities

def test_capabilities_keys_and_contents(tmp_path):
    server = _server(tmp_path)
    library = SkillLibrary(tmp_path / "home")
    library.add(Skill(name="adder", description="adds things"))
    payload = server.handle_capabilities()
    assert set(payload) == {"tools", "skills", "mcp_servers"}
    # Tools are rows, not bare names: the capabilities page needs
    # origin and side-effect badges to tell a core tool from an
    # adopted plugin that acts on the world.
    rows = {row["name"]: row for row in payload["tools"]}
    assert "write" in rows
    assert rows["write"]["origin"] == "core"
    assert rows["write"]["side_effect"] is False
    assert "adder" in payload["skills"]
    assert payload["mcp_servers"] == []


def test_capabilities_route_live(live_server):
    base, _, _ = live_server
    status, payload = _get(base, "/api/capabilities")
    assert status == 200
    assert "write" in [row["name"] for row in payload["tools"]]


def test_capability_code_known_and_unknown(tmp_path):
    server = _server(tmp_path)
    found = server.handle_capability_code("lookup_capability")
    assert found is not None
    assert found["name"] == "lookup_capability"
    assert "def lookup_capability" in found["code"]
    assert server.handle_capability_code("no-such-handler-xyz") is None
    assert server.handle_capability_code("") is None


def test_capability_code_route_live(live_server):
    base, _, _ = live_server
    status, payload = _get(base, "/api/capabilities/code?name=lookup_capability")
    assert status == 200 and "def lookup_capability" in payload["code"]
    status, _ = _get(base, "/api/capabilities/code?name=no-such-handler-xyz")
    assert status == 404


# ------------------------------------------------------------------- memory

def test_memory_points_and_delete(tmp_path):
    store = InMemoryStore()
    store.upsert("episodic", Point(id="ep1", text="episode one", vector=[],
                                    payload={"text": "episode one"}))
    store.upsert("knowledge", Point(id="k1", text="fact one", vector=[],
                                     payload={"text": "fact one"}))
    server = _server(tmp_path, memory=_FakeMemory(store))
    episodes = server.handle_memory_points("episodic")
    assert [p["id"] for p in episodes["points"]] == ["ep1"]
    assert episodes["points"][0]["text"] == "episode one"
    assert [p["id"] for p in server.handle_memory_points("knowledge")["points"]] == ["k1"]
    assert server.handle_memory_point_delete("episodic", "ep1") == {"ok": True}
    assert server.handle_memory_points("episodic") == {"points": []}
    assert server.handle_memory_point_delete("episodic", "ghost") == {"ok": False}


def test_memory_points_without_memory_and_limit_cap(tmp_path):
    server = _server(tmp_path, memory=None)
    assert server.handle_memory_points("episodic") == {"points": []}
    assert server.handle_memory_point_delete("episodic", "x") == {"ok": False}
    store = InMemoryStore()
    for i in range(5):
        store.upsert("episodic", Point(id=f"p{i}", text=f"t{i}", vector=[],
                                        payload={"text": f"t{i}"}))
    server = _server(tmp_path, memory=_FakeMemory(store))
    assert len(server.handle_memory_points("episodic", limit=2)["points"]) == 2
    assert len(server.handle_memory_points("episodic", limit=9999)["points"]) == 5


def test_memory_routes_live(live_server):
    base, server, _ = live_server
    # No memory configured: empty lists, delete is 404. The route
    # answers a paginated envelope; "points" is kept for older
    # clients, "items" is what the memory page renders.
    status, payload = _get(base, "/api/memory/episodes")
    assert status == 200
    assert payload["points"] == [] and payload["items"] == []
    assert payload["total"] == 0 and payload["matched"] == 0
    status, _ = _delete(base, "/api/memory/episodes/ghost")
    assert status == 404
    # With memory: list and delete round-trip.
    store = InMemoryStore()
    store.upsert("knowledge", Point(id="k9", text="fact", vector=[],
                                     payload={"text": "fact"}))
    server.memory = _FakeMemory(store)
    status, payload = _get(base, "/api/memory/knowledge")
    assert status == 200 and [p["id"] for p in payload["points"]] == ["k9"]
    status, payload = _delete(base, "/api/memory/knowledge/k9")
    assert status == 200 and payload == {"ok": True}
    status, payload = _get(base, "/api/memory/knowledge")
    assert payload["points"] == [] and payload["items"] == []
