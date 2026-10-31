"""MCP $VAR secret expansion: values expand from the environment
at connect time, and the variable name is exposed for display.

The $VAR form is the recommended form for a token: the secret
stays in the environment, the expanded value is what travels
in the header, and an approval card can show the variable
name instead of the secret itself.
"""

import json
import os

import pytest

from src.interface.mcp_client import McpClient, expand_env_value as reexported
from src.interface.mcp_http import McpHttpClient, expand_env_value


def test_expand_exact_var(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    assert expand_env_value("$MCP_TEST_TOKEN") == ("tok-123", "MCP_TEST_TOKEN")


def test_expand_braced_var(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    assert expand_env_value("${MCP_TEST_TOKEN}") == ("tok-123", "MCP_TEST_TOKEN")


def test_expand_bearer_var_inside_larger_string(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    expanded, var = expand_env_value("Bearer $MCP_TEST_TOKEN")
    assert expanded == "Bearer tok-123"
    assert var == "MCP_TEST_TOKEN"


def test_expand_multiple_refs_first_var_reported(monkeypatch):
    monkeypatch.setenv("MCP_A", "aaa")
    monkeypatch.setenv("MCP_B", "bbb")
    expanded, var = expand_env_value("$MCP_A-$MCP_B")
    assert expanded == "aaa-bbb"
    assert var == "MCP_A"


def test_expand_no_reference_unchanged(monkeypatch):
    assert expand_env_value("plain-token") == ("plain-token", None)
    assert expand_env_value("") == ("", None)


def test_expand_unset_variable_is_empty_not_error(monkeypatch):
    monkeypatch.delenv("MCP_DEFINITELY_UNSET_XYZ", raising=False)
    assert expand_env_value("$MCP_DEFINITELY_UNSET_XYZ") == ("", "MCP_DEFINITELY_UNSET_XYZ")


def test_expand_reexported_from_mcp_client():
    from src.interface.mcp_http import expand_env_value as http_expand
    assert reexported is http_expand


class _FakeResponse:
    """Minimal urlopen result: JSON-RPC result + headers mapping."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()
        self.headers = {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_urlopen(monkeypatch, seen):
    import src.interface.mcp_http as http_mod

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _FakeResponse({"jsonrpc": "2.0", "id": 1,
                              "result": {"serverInfo": {"name": "demo"}}})

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)


def test_http_client_var_token_sends_expanded_header(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    seen = []
    _mock_urlopen(monkeypatch, seen)
    client = McpHttpClient("http://127.0.0.1:9/mcp", token="$MCP_TEST_TOKEN")
    assert client.token_var == "MCP_TEST_TOKEN"
    assert client.token == "tok-123"
    client.initialize()
    assert seen[0].headers["Authorization"] == "Bearer tok-123"


def test_http_client_bearer_var_token(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    seen = []
    _mock_urlopen(monkeypatch, seen)
    client = McpHttpClient("http://127.0.0.1:9/mcp", token="Bearer $MCP_TEST_TOKEN")
    # The header adds "Bearer " itself, so the leading scheme is
    # stripped from the expanded token rather than doubled.
    assert client.token == "tok-123"
    assert client.token_var == "MCP_TEST_TOKEN"
    client.initialize()
    assert seen[0].headers["Authorization"] == "Bearer tok-123"


def test_http_client_literal_token_has_no_token_var():
    client = McpHttpClient("http://127.0.0.1:9/mcp", token="literal-token")
    assert client.token == "literal-token"
    assert client.token_var is None


def test_http_client_unset_var_token_has_no_header(monkeypatch):
    monkeypatch.delenv("MCP_DEFINITELY_UNSET_XYZ", raising=False)
    seen = []
    _mock_urlopen(monkeypatch, seen)
    client = McpHttpClient("http://127.0.0.1:9/mcp", token="$MCP_DEFINITELY_UNSET_XYZ")
    assert client.token is None
    assert client.token_var == "MCP_DEFINITELY_UNSET_XYZ"
    client.initialize()
    assert "Authorization" not in seen[0].headers


def test_token_in_arguments_refusal_still_works_with_expanded_token(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    client = McpHttpClient("http://127.0.0.1:9/mcp", token="$MCP_TEST_TOKEN")
    with pytest.raises(ValueError, match="contains the auth token"):
        client.call_tool("demo", {"leak": "tok-123"})
    # A clean argument is not refused by the check itself (the
    # network call would fail on the dead endpoint, so only the
    # check is exercised here via _check_arguments).
    client._check_arguments({"clean": "hello"})


def test_stdio_client_env_expansion(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    client = McpClient(["npx", "demo-mcp"],
                       env={"MCP_TOKEN": "$MCP_TEST_TOKEN",
                            "PLAIN": "literal"})
    assert client.env == {"MCP_TOKEN": "tok-123", "PLAIN": "literal"}
    assert client.env_vars == {"MCP_TOKEN": "MCP_TEST_TOKEN", "PLAIN": None}
    assert client.token_var == "MCP_TEST_TOKEN"


def test_stdio_client_env_reaches_server_process(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    monkeypatch.setenv("UNRELATED_PRIVATE_KEY", "must-not-reach-server")
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import json, sys, os\n"
        "assert 'UNRELATED_PRIVATE_KEY' not in os.environ\n"
        "assert os.environ.get('MCP_TOKEN') == 'tok-123'\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': req['id'],"
        " 'result': {}}) + '\\n')\n"
        "    sys.stdout.flush()\n")
    client = McpClient(["python3", str(script)],
                       env={"MCP_TOKEN": "$MCP_TEST_TOKEN"})
    try:
        client.start()
        assert client.env["MCP_TOKEN"] == "tok-123"
        assert client.token_var == "MCP_TEST_TOKEN"
    finally:
        client.close()


def test_stdio_timeout_closes_unresponsive_server(tmp_path):
    import sys
    script = tmp_path / "silent.py"
    script.write_text("import time\ntime.sleep(30)\n")
    client = McpClient([sys.executable, str(script)], timeout=0.05)
    with pytest.raises(TimeoutError):
        client.start()
    assert client.proc is None


def test_stdio_notification_and_wrong_id(tmp_path):
    import sys
    script = tmp_path / "server.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " print(json.dumps({'jsonrpc':'2.0','method':'notice'}),flush=True)\n"
        " print(json.dumps({'jsonrpc':'2.0','id':r['id'] if r['id']==1 else 99,'result':{}}),flush=True)\n")
    client = McpClient([sys.executable, str(script)], timeout=1)
    try:
        client.start()
        with pytest.raises(ValueError, match="ID mismatch"):
            client.list_tools()
    finally:
        client.close()


def test_stdio_write_to_nonreading_server_times_out(tmp_path):
    import sys
    script = tmp_path / "stalled.py"
    script.write_text(
        "import json,sys,time\n"
        "request=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}),flush=True)\n"
        "time.sleep(30)\n")
    client = McpClient([sys.executable, str(script)], timeout=0.2)
    client.start()
    with pytest.raises(TimeoutError, match="write timed out"):
        client.call_tool("test", {"data": "x" * 500000})
    assert client.proc is None
