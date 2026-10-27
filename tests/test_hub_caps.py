"""Hub run caps and base64 elision."""

from src.interface.webui import HUB_MAX_EVENTS, WebUIServer


def _server(tmp_path):
    return WebUIServer(tmp_path, memory=None)


def test_cap_constant_is_500():
    assert HUB_MAX_EVENTS == 500


def test_appending_600_events_keeps_newest_500(tmp_path):
    server = _server(tmp_path)
    server.handle_hub_run_create({"id": "run1", "goal": "g"})
    for i in range(600):
        server.handle_hub_run_event("run1", {"n": i})
    record = server._read_json_file(server._hub_run_path("run1"))
    events = record["events"]
    assert len(events) == 500
    assert events[0]["n"] == 100
    assert events[-1]["n"] == 599


def test_cap_applies_to_stages_and_traces(tmp_path):
    server = _server(tmp_path)
    server.handle_hub_run_create({"id": "run1", "goal": "g"})
    for i in range(510):
        server.handle_hub_run_stage("run1", {"n": i})
        server.handle_hub_run_trace("run1", {"n": i})
    record = server._read_json_file(server._hub_run_path("run1"))
    assert len(record["stages"]) == 500
    assert len(record["traces"]) == 500
    assert record["stages"][0]["n"] == 10


def test_finish_elides_nested_base64_and_leaves_other_text(tmp_path):
    server = _server(tmp_path)
    server.handle_hub_run_create({"id": "run1", "goal": "g"})
    long_b64 = "QUJD" * 300  # 1200 chars, base64 alphabet, multiple of 4
    long_text = ("hello world, this is plain text with spaces. " * 40)  # >1000, not base64
    short_b64 = "QUJD" * 10
    server.handle_hub_run_event("run1", {"payload": {"image": long_b64, "note": "hi",
                                                     "long_text": long_text,
                                                     "short": short_b64}})
    result = server.finish_hub_run("run1")
    assert result == {"ok": True, "id": "run1", "status": "done"}
    record = server._read_json_file(server._hub_run_path("run1"))
    assert record["status"] == "done"
    event = record["events"][0]["payload"]
    assert event["image"] == "<elided:base64>"
    assert event["note"] == "hi"
    assert event["long_text"] == long_text
    assert event["short"] == short_b64


def test_elide_does_not_mutate_input():
    server = WebUIServer.__new__(WebUIServer)
    data = {"image": "QUJD" * 300, "items": ["QUJD" * 300, "keep"]}
    out = WebUIServer._elide_base64(data)
    assert out["image"] == "<elided:base64>"
    assert out["items"][0] == "<elided:base64>"
    assert out["items"][1] == "keep"
    assert data["image"] == "QUJD" * 300
    assert data["items"][0] == "QUJD" * 300


def test_finish_unknown_run_returns_ok_false(tmp_path):
    server = _server(tmp_path)
    assert server.finish_hub_run("nope")["ok"] is False
