"""A hub over HTTP, the remote direction the local hub never had.

The local hub is a directory on one machine. This module is the
small server that lets another machine push and pull the same
records over HTTP: DAG workflows and mission records, stored as
JSON files under <home>/hub/dags/ and <home>/hub/missions/.
It is stdlib only, localhost by default, and deliberately
narrow: no plugin code is served or executed here, only the
JSON records Aidon already stores locally.

Auth, fail closed: when a token is set, every route requires
``Authorization: Bearer <token>`` exactly, and anything else
gets 401. When no token is set, the server is unauthenticated
and is meant for localhost only, like the WebUI default.

HubClient is the matching client, urllib only, with the four
operations a caller needs: push_dag, pull_dag, list_dags, and
push_mission, plus list/pull for missions for symmetry.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _safe_id(value: str) -> str | None:
    value = str(value or "")
    if not _SAFE_ID.match(value) or value in (".", ".."):
        return None
    return value


class HubServer:
    """Serve DAG and mission JSON records from <home>/hub/."""

    def __init__(self, home, token: str | None = None):
        self.home = Path(home)
        self.token = token or None
        self.dags_dir = self.home / "hub" / "dags"
        self.missions_dir = self.home / "hub" / "missions"
        self.dags_dir.mkdir(parents=True, exist_ok=True)
        self.missions_dir.mkdir(parents=True, exist_ok=True)

    def _authorized(self, headers) -> bool:
        if not self.token:
            return True
        return headers.get("authorization", "") == f"Bearer {self.token}"

    def _store(self, directory: Path, record: dict) -> dict:
        record_id = _safe_id(record.get("id", ""))
        if record_id is None:
            return {"ok": False, "error": "record needs a safe id"}
        path = directory / f"{record_id}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {"ok": True, "id": record_id}

    def _list(self, directory: Path) -> list[str]:
        return sorted(p.stem for p in directory.glob("*.json"))

    def _get(self, directory: Path, record_id: str):
        safe = _safe_id(record_id)
        if safe is None:
            return None
        path = directory / f"{safe}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                return body if isinstance(body, dict) else {}

            def _auth(self) -> bool:
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not server._authorized(headers):
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                path = self.path.split("?", 1)[0]
                if path == "/health":
                    self._send(200, {"ok": True})
                elif path == "/dags":
                    self._send(200, {"dags": server._list(server.dags_dir)})
                elif path.startswith("/dags/"):
                    record = server._get(server.dags_dir, path[len("/dags/"):])
                    self._send(200, record) if record is not None else \
                        self._send(404, {"error": "dag not found"})
                elif path == "/missions":
                    self._send(200, {"missions": server._list(server.missions_dir)})
                elif path.startswith("/missions/"):
                    record = server._get(server.missions_dir, path[len("/missions/"):])
                    self._send(200, record) if record is not None else \
                        self._send(404, {"error": "mission not found"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return
                path = self.path.split("?", 1)[0]
                body = self._read_body()
                if path == "/dags":
                    result = server._store(server.dags_dir, body)
                    self._send(200 if result["ok"] else 400, result)
                elif path == "/missions":
                    result = server._store(server.missions_dir, body)
                    self._send(200 if result["ok"] else 400, result)
                else:
                    self._send(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        return Handler

    def start(self, host: str = "127.0.0.1", port: int = 0):
        """Serve on a thread. Returns (httpd, thread, base_url)."""
        httpd = ThreadingHTTPServer((host, port), self.make_handler())
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread, f"http://{host}:{httpd.server_address[1]}"

    def serve(self, host: str = "127.0.0.1", port: int = 8003) -> None:
        ThreadingHTTPServer((host, port), self.make_handler()).serve_forever()


class HubClient:
    """Client for a HubServer, urllib only."""

    def __init__(self, base_url: str, token: str | None = None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"hub {method} {path} -> HTTP {exc.code}: {detail}") from exc

    def health(self) -> dict:
        return self._request("GET", "/health")

    def push_dag(self, dag: dict) -> dict:
        return self._request("POST", "/dags", dag)

    def pull_dag(self, dag_id: str) -> dict:
        return self._request("GET", f"/dags/{dag_id}")

    def list_dags(self) -> list[str]:
        return self._request("GET", "/dags").get("dags", [])

    def push_mission(self, mission: dict) -> dict:
        return self._request("POST", "/missions", mission)

    def pull_mission(self, mission_id: str) -> dict:
        return self._request("GET", f"/missions/{mission_id}")

    def list_missions(self) -> list[str]:
        return self._request("GET", "/missions").get("missions", [])
