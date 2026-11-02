"""Exercise the browser callback through token exchange without a provider."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from src.interface import mcp_oauth


def test_successful_callback_is_exchanged_and_cannot_be_replayed(tmp_path, monkeypatch):
    endpoint = "https://mcp.example/mcp"
    issuer = "https://auth.example"
    manager = mcp_oauth.McpOAuthManager(tmp_path, "http://127.0.0.1:8765/oauth/mcp/callback")
    monkeypatch.setattr(mcp_oauth, "_resource_metadata", lambda url, challenge: ({"resource": endpoint}, issuer))
    monkeypatch.setattr(mcp_oauth, "_authorization_metadata", lambda url: {
        "issuer": issuer, "registration_endpoint": issuer + "/register",
        "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token"})
    monkeypatch.setattr(mcp_oauth, "_json_request", lambda *args, **kwargs: {"client_id": "client"})
    exchanged = {}

    def token_post(url, form):
        exchanged.update(form)
        return {"access_token": "example-access", "refresh_token": "example-refresh"}

    monkeypatch.setattr(manager, "_token_post", token_post)

    def approve(kind, title, body):
        link = next(line.removeprefix("Authorize: ") for line in body.splitlines()
                    if line.startswith("Authorize: "))
        state = parse_qs(urlsplit(link).query)["state"][0]
        assert manager.finish_callback({"state": [state], "code": ["example-code"]})[0]
        assert not manager.finish_callback({"state": [state], "code": ["replayed"]})[0]
        return SimpleNamespace(verdict="approved")

    assert manager.authorize(endpoint, "", approve) == "example-access"
    assert exchanged["code"] == "example-code"
    assert exchanged["code_verifier"]
    assert manager.access_token(endpoint) == "example-access"
    assert not manager.pending
