"""OAuth 2.1 authorization-code flow for remote MCP HTTP servers.

This is a public-client flow: HTTPS metadata, dynamic client registration,
PKCE S256, one-time state, a loopback WebUI callback, and private token files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class OAuthError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_safe_opener = urllib.request.build_opener(_NoRedirect())


def _https_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OAuthError("OAuth metadata and endpoints must use HTTPS")
    return str(url)


def _json_request(url, *, method="GET", payload=None, headers=None):
    _https_url(url)
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "User-Agent": "Aidon-MCP-OAuth",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    try:
        with _safe_opener.open(request, timeout=15) as response:
            _https_url(response.geturl())
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"OAuth endpoint returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError("OAuth endpoint is unreachable") from exc
    if len(raw) > 1024 * 1024:
        raise OAuthError("OAuth metadata response is too large")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("OAuth endpoint returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise OAuthError("OAuth endpoint returned invalid metadata")
    return result


def _well_known(issuer: str, suffix: str) -> str:
    parsed = urllib.parse.urlsplit(_https_url(issuer))
    path = parsed.path.rstrip("/")
    if path:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
            "/.well-known/" + suffix + path, "", ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
        "/.well-known/" + suffix, "", ""))


def _resource_metadata(resource_url: str, challenge: str) -> tuple[dict, str]:
    match = re.search(r'resource_metadata="([^"]+)"', challenge or "", re.I)
    if match:
        metadata_url = match.group(1)
    else:
        parsed = urllib.parse.urlsplit(_https_url(resource_url))
        metadata_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
            "/.well-known/oauth-protected-resource" + parsed.path.rstrip("/"), "", ""))
    metadata = _json_request(metadata_url)
    if metadata.get("resource") and metadata["resource"] != resource_url:
        raise OAuthError("Protected-resource metadata names a different MCP server")
    servers = metadata.get("authorization_servers") or []
    if not servers:
        raise OAuthError("MCP server did not publish an authorization server")
    return metadata, _https_url(servers[0])


def _authorization_metadata(issuer: str) -> dict:
    metadata = _json_request(_well_known(issuer, "oauth-authorization-server"))
    if metadata.get("issuer") != issuer:
        raise OAuthError("Authorization-server issuer does not match its metadata URL")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if key not in metadata:
            raise OAuthError(f"Authorization server is missing {key}")
        _https_url(metadata[key])
    return metadata


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_managers = {}
_managers_lock = threading.Lock()


def get_oauth_manager(home, callback_uri="http://127.0.0.1:8765/oauth/mcp/callback"):
    key = str(Path(home).resolve())
    with _managers_lock:
        manager = _managers.get(key)
        if manager is None:
            manager = McpOAuthManager(Path(home), callback_uri)
            _managers[key] = manager
        else:
            manager.callback_uri = callback_uri
        return manager


def callback_uri_from_env():
    """Return the loopback callback matching the WebUI listener."""
    value = os.environ.get("AIDON_MCP_OAUTH_REDIRECT_URI", "").strip()
    if value:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in
                ("127.0.0.1", "localhost", "::1") or parsed.path != "/oauth/mcp/callback"
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise OAuthError("AIDON_MCP_OAUTH_REDIRECT_URI must be a loopback callback URL")
        return value
    try:
        port = str(int(os.environ.get("AIDON_WEBUI_PORT", "8765")))
    except (TypeError, ValueError):
        port = "8765"
    return f"http://127.0.0.1:{port}/oauth/mcp/callback"


class McpOAuthManager:
    def __init__(self, home: Path, callback_uri: str):
        self.path = home / "credentials" / "mcp_oauth.json"
        self.callback_uri = callback_uri
        self._lock = threading.RLock()
        self.pending = {}
        self.records = {}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.records = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.records, dict):
                self.records = {}
        except (OSError, json.JSONDecodeError):
            self.records = {}
        try:
            self.path.parent.chmod(0o700)
            if self.path.exists():
                self.path.chmod(0o600)
        except OSError:
            pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.records, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, self.path)
        try:
            self.path.parent.chmod(0o700)
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _key(resource_url):
        return hashlib.sha256(resource_url.rstrip("/").encode()).hexdigest()

    def access_token(self, resource_url):
        key = self._key(resource_url)
        with self._lock:
            record = self.records.get(key)
            if not record:
                return ""
            tokens = record.get("tokens", {})
            if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 30:
                return tokens["access_token"]
            refresh = tokens.get("refresh_token")
            token_endpoint = record.get("token_endpoint")
            if not refresh or not token_endpoint:
                return tokens.get("access_token", "")
            try:
                fresh = self._token_post(token_endpoint, {
                    "grant_type": "refresh_token", "refresh_token": refresh,
                    "client_id": record["client_id"],
                    "resource": record.get("resource", resource_url)})
                self._save_tokens(key, fresh, record)
                return fresh.get("access_token", "")
            except OAuthError:
                return ""

    def _token_post(self, endpoint, form):
        request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(form).encode(),
            method="POST", headers={"Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Aidon-MCP-OAuth"})
        try:
            with _safe_opener.open(request, timeout=20) as response:
                _https_url(response.geturl())
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise OAuthError(f"OAuth token endpoint returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OAuthError("OAuth token endpoint is unreachable") from exc
        if len(raw) > 1024 * 1024:
            raise OAuthError("OAuth token response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthError("OAuth token endpoint returned invalid JSON") from exc
        if not isinstance(result, dict) or not result.get("access_token"):
            raise OAuthError("OAuth token endpoint did not return an access token")
        return result

    def _save_tokens(self, key, tokens, record):
        previous = record.get("tokens", {})
        saved = {name: tokens[name] for name in
                 ("access_token", "refresh_token", "token_type", "scope")
                 if tokens.get(name)}
        if not saved.get("refresh_token") and previous.get("refresh_token"):
            saved["refresh_token"] = previous["refresh_token"]
        saved["expires_at"] = time.time() + max(0, int(tokens.get("expires_in", 3600)))
        record["tokens"] = saved
        self.records[key] = record
        self._save()

    def authorize(self, resource_url, challenge, approve):
        resource, issuer = _resource_metadata(resource_url, challenge)
        metadata = _authorization_metadata(issuer)
        callback = urllib.parse.urlsplit(self.callback_uri)
        if callback.scheme != "http" or callback.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise OAuthError("MCP OAuth callback must use a loopback HTTP address")
        state = secrets.token_urlsafe(32)
        verifier = _b64url(secrets.token_bytes(32))
        code_challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        key = self._key(resource_url)
        existing = self.records.get(key, {})
        client_id = existing.get("client_id")
        if not client_id:
            registration = _json_request(metadata["registration_endpoint"], method="POST", payload={
                "client_name": "Aidon", "redirect_uris": [self.callback_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "token_endpoint_auth_method": "none"})
            client_id = registration.get("client_id")
            if not client_id:
                raise OAuthError("OAuth dynamic registration did not return a client ID")
        params = {"client_id": client_id, "redirect_uri": self.callback_uri,
                  "response_type": "code", "state": state,
                  "code_challenge": code_challenge, "code_challenge_method": "S256",
                  "resource": resource.get("resource", resource_url)}
        scopes = resource.get("scopes_supported") or metadata.get("scopes_supported") or []
        requested_scope = re.search(r'\bscope="([^"]+)"', challenge or "", re.I)
        if requested_scope:
            scopes = requested_scope.group(1).split()
        if scopes:
            params["scope"] = " ".join(str(scope) for scope in scopes)
        auth_url = metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
        event = threading.Event()
        pending = {"state": state, "event": event, "verifier": verifier,
                   "resource_url": resource_url, "resource": resource,
                   "metadata": metadata, "client_id": client_id,
                   "auth_url": auth_url, "result": None, "created": time.time()}
        with self._lock:
            self.pending[state] = pending
        try:
            return self._complete_authorization(pending, key, issuer, approve)
        finally:
            with self._lock:
                self.pending.pop(state, None)

    def _complete_authorization(self, pending, key, issuer, approve):
        resource_url = pending["resource_url"]
        resource = pending["resource"]
        metadata = pending["metadata"]
        client_id = pending["client_id"]
        verifier = pending["verifier"]
        decision = approve("install", "Authorize MCP account",
            "Aidon needs your authorization to connect this MCP server.\n"
            "Open the provider link first, sign in, and return here. Then approve this card "
            "so Aidon can exchange the code and connect:\n\n"
            "Authorize: " + pending["auth_url"] + "\n\n"
            "Callback: " + self.callback_uri + "\n"
            "Aidon uses OAuth authorization code with PKCE. Tokens are stored in a private file in Aidon's home. "
            "This approval authorizes the connection only; remote tool actions keep their own approval gates.")
        if getattr(decision, "verdict", None) not in ("approved", "auto_approved"):
            raise OAuthError("MCP OAuth authorization was rejected")
        if not pending["event"].wait(600):
            raise OAuthError("MCP OAuth callback timed out")
        result = pending.get("result")
        if not result or result.get("error") or not result.get("code"):
            raise OAuthError("OAuth authorization was cancelled or rejected")
        form = {"grant_type": "authorization_code", "code": result["code"],
                "redirect_uri": self.callback_uri, "client_id": client_id,
                "code_verifier": verifier,
                "resource": resource.get("resource", resource_url)}
        token_result = self._token_post(metadata["token_endpoint"], form)
        saved = {"client_id": client_id, "token_endpoint": metadata["token_endpoint"],
                 "issuer": issuer, "resource": resource.get("resource", resource_url),
                 "tokens": {}}
        with self._lock:
            self._save_tokens(key, token_result, saved)
        return token_result["access_token"]

    def finish_callback(self, query):
        state = (query.get("state") or [""])[0]
        with self._lock:
            pending = self.pending.get(state)
            if not pending or pending["result"] is not None or time.time() - pending["created"] > 600:
                return False, "This OAuth callback is invalid or expired. Return to Aidon and try again."
            code = (query.get("code") or [""])[0]
            error = (query.get("error") or [""])[0]
            if not code and not error:
                return False, "The OAuth provider did not return an authorization code."
            expected_issuer = pending["metadata"].get("issuer")
            returned_issuer = (query.get("iss") or [""])[0]
            if returned_issuer and returned_issuer != expected_issuer:
                pending["result"] = {"error": "issuer mismatch"}
            else:
                pending["result"] = {"code": code, "error": error}
            pending["event"].set()
            accepted = bool(code and not pending["result"].get("error"))
            return (accepted, "Authorization received. Return to Aidon to finish connecting." if accepted
                    else "Authorization was declined or invalid. Return to Aidon.")
