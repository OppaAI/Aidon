"""WebUI file serving and chat markdown rendering.

Inventory extras let an absolute-path deliverable show up in the
evidence listing; /api/files serves a file from under the server's
home directory, same-origin, and refuses anything outside it; the
chat page renders markdown (tables, bold, code, links) once typing
completes. The markdown tests run the real render_markdown from
CHAT_HTML under node, so they cannot drift from the shipped code.
"""

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from src.agentic.artifact_inventory import inventory, inventory_markdown
from src.interface.webui import CHAT_HTML, WebUIServer


# ---------------------------------------------------------------- inventory

def test_inventory_includes_absolute_extra_outside_workdir(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "local.md").write_text("local\n")
    outside = tmp_path / "elsewhere" / "haiku.txt"
    outside.parent.mkdir()
    outside.write_text("the haiku\n")
    rows = inventory(workdir, extra=[str(outside)])
    by_path = {row["path"]: row for row in rows}
    assert str(outside) in by_path
    assert by_path[str(outside)]["absolute"] is True
    assert by_path[str(outside)]["kind"] == "text"
    assert by_path["local.md"]["absolute"] is False


def test_inventory_extra_relative_skips_missing_and_dedupes(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "local.md").write_text("local\n")
    rows = inventory(workdir, extra=["local.md", "nope-xyz.txt"])
    assert [row["path"] for row in rows] == ["local.md"]


def test_inventory_markdown_with_absolute_extra(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "haiku.txt"
    outside.write_text("the haiku\n")
    md = inventory_markdown(workdir, extra=[str(outside)])
    assert str(outside) in md
    assert "total (1 files)" in md


# ---------------------------------------------------------------- file route

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
        yield base, home
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(base, path):
    url = base + "/api/files?path=" + urllib.parse.quote(path, safe="")
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_lab_session_http_persists_and_rejects_stale_revision(live_server):
    base, _ = live_server
    state = {"id": "chat", "turns": [{"role": "user", "text": "Help with video"}]}
    body = json.dumps({"kind": "tool", "state": state, "revision": 0}).encode()
    request = urllib.request.Request(base + "/api/lab-session", body,
                                     {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        assert json.load(response)["revision"] == 1
    with urllib.request.urlopen(base + "/api/lab-session?kind=tool") as response:
        assert json.load(response) == {"revision": 1, "state": state}
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 409


def test_file_route_serves_file_under_home_with_md_type(live_server):
    base, home = live_server
    target = home / "workspace" / "notes.md"
    target.parent.mkdir()
    target.write_text("# hello\n")
    status, ctype, body = _get(base, "workspace/notes.md")
    assert status == 200
    assert body == b"# hello\n"
    assert ctype == "text/markdown"


def test_file_route_serves_absolute_path_under_home(live_server):
    base, home = live_server
    target = home / "workspace" / "sub" / "data.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"a": 1}')
    status, ctype, body = _get(base, str(target))
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"a": 1}


def test_file_route_rejects_traversal(live_server):
    base, home = live_server
    status, _, _ = _get(base, "../outside.txt")
    assert status == 403
    status, _, _ = _get(base, "/etc/passwd")
    assert status == 403


def test_file_route_missing_is_404_and_directory_is_400(live_server):
    base, home = live_server
    (home / "workspace" / "sub").mkdir(parents=True)
    assert _get(base, "workspace/nope-xyz.txt")[0] == 404
    assert _get(base, "workspace/sub")[0] == 400


def test_file_route_rejects_private_home_files(live_server):
    base, home = live_server
    (home / ".env").write_text("SECRET=value")
    (home / "registry.db").write_text("private")
    assert _get(base, ".env")[0] == 403
    assert _get(base, "registry.db")[0] == 403


@pytest.mark.parametrize("length,expected", [("-1", 400), ("not-a-number", 400), (str(81 * 1024 * 1024), 413)])
def test_request_body_length_is_bounded(live_server, length, expected):
    import http.client
    base, _ = live_server
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        conn.putrequest("POST", "/api/chat")
        conn.putheader("Content-Length", length)
        conn.endheaders()
        assert conn.getresponse().status == expected
    finally:
        conn.close()


def test_file_route_refuses_symlink_escaping_home(live_server, tmp_path):
    base, home = live_server
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    (home / "link.txt").symlink_to(secret)
    assert _get(base, "link.txt")[0] == 403


def test_chat_image_attachment_survives_history_reload_and_is_served(live_server):
    from src.memory.conversations_db import ConversationStoreDB

    base, home = live_server
    image = home / "library" / "uploads" / "photo.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake-jpeg")
    db = ConversationStoreDB(home)
    conversation = db.create_conversation()["id"]
    db.record_exchange(conversation, "user", "What is in this picture?")
    exchange_no = db.latest_exchange_no(conversation, "user")
    attachment = db.add_attachment(conversation, str(image), kind="image",
                                   exchange_no=exchange_no)

    # Exercise the same restore path as a page refresh, then fetch only by
    # opaque attachment ID (the HTTP request cannot provide a filesystem path).
    from src.interface.webui import WebUIServer
    server = WebUIServer(home, memory=None)
    restored = server.handle_conversation_get(conversation)
    assert restored["attachments"][0]["name"] == "photo.jpg"
    assert restored["attachments"][0]["exchange_no"] == exchange_no
    path = restored["attachments"][0]["url"]
    with urllib.request.urlopen(base + path) as response:
        assert response.status == 200
        assert response.headers.get("Content-Type") == "image/jpeg"
        assert response.read() == b"fake-jpeg"
    db.close()


def test_chat_attachment_route_does_not_allow_path_traversal(live_server):
    from src.memory.conversations_db import ConversationStoreDB

    base, home = live_server
    outside = home.parent / "outside.jpg"
    outside.write_bytes(b"private")
    db = ConversationStoreDB(home)
    conversation = db.create_conversation()["id"]
    db.add_attachment(conversation, str(outside), kind="image")
    row = db.attachments(conversation)[0]
    try:
        with urllib.request.urlopen(base + "/api/conversation-attachments/" + row["id"] + "/file"):
            pytest.fail("outside path should not be served")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    db.close()


# ---------------------------------------------------------------- markdown

def _extract_fn(source: str, name: str) -> str:
    start = source.index("function " + name + "(")
    i = source.index("{", start)
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
    raise AssertionError(f"unbalanced function {name}")


def _run_markdown(sample: str) -> str:
    """Run the real render_markdown from CHAT_HTML under node."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    fns = "\n".join(_extract_fn(CHAT_HTML, name)
                     for name in ("escapeHtml", "renderInline", "render_markdown"))
    harness = fns + "\nconsole.log(render_markdown(" + json.dumps(sample) + "));\n"
    proc = subprocess.run(["node", "-e", harness], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n")


def test_chat_page_contains_render_markdown():
    assert "function render_markdown(" in CHAT_HTML
    for marker in ("<table>", "<strong>", "<code>", '<a href='):
        assert marker in CHAT_HTML
    # The karaoke completion hook re-renders the finished message.
    assert "element.innerHTML = render_markdown(text);" in CHAT_HTML


def test_render_markdown_table_bold_code_link_and_escape():
    sample = ("| Item | Cost |\n| --- | ---: |\n| **Beef** | `42` |\n"
              "See [shop](https://example.com) and <b>not bold</b>")
    html = _run_markdown(sample)
    assert "<table>" in html
    assert "<th>Item</th>" in html and "<th>Cost</th>" in html
    assert "<td><strong>Beef</strong></td>" in html
    assert "<td><code>42</code></td>" in html
    assert '<a href="https://example.com">shop</a>' in html
    # Input HTML is escaped, never passed through as markup.
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html
    assert "<b>not bold</b>" not in html
