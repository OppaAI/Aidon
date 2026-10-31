"""MCP health sweep script: runs standalone against a home,
reports per-server health, and never fails on down servers."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "mcp_health_sweep.py"

FAKE_SERVER = '''\
import json
import sys
for line in sys.stdin:
    try:
        req = json.loads(line)
    except Exception:
        continue
    method = req.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo_tool",
                             "description": "Echo."}]}
    else:
        result = {}
    sys.stdout.write(json.dumps(
        {"jsonrpc": "2.0", "id": req.get("id"), "result": result}) + "\\n")
    sys.stdout.flush()
'''


def _run(home, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), *extra],
        capture_output=True, text=True, timeout=60)


def test_sweep_empty_home_exits_zero(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "SWEEP DONE: 0 servers" in proc.stdout


def test_sweep_fake_server_records_healthy(tmp_path):
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER)
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp_servers.json").write_text(json.dumps({
        "fake": {"transport": "stdio", "command": "python3",
                 "args": [str(server)]}}))
    proc = _run(home)
    assert proc.returncode == 0, proc.stderr
    assert "fake: healthy" in proc.stdout
    cache = json.loads(
        (home / "capability_hub_health.json").read_text())
    assert cache["mcp:fake"]["health"] == "healthy"
