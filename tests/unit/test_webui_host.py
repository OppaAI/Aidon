"""Host validation for the WebUI: a rebinding domain that resolves to
127.0.0.1 must not pass the Origin check by sending a matching Host."""

from src.interface.webui_auth import is_allowed_host


def test_loopback_hosts_allowed():
    for host in ["127.0.0.1:8765", "localhost:8765", "LOCALHOST", "127.0.0.2",
                 "[::1]:8765", "::1"]:
        assert is_allowed_host(host), host


def test_foreign_hosts_rejected():
    for host in ["", "evil.com", "evil.com:8765", "192.168.1.5:8765",
                 "aidon.evil.com"]:
        assert not is_allowed_host(host), host


def test_configured_hosts_allowed():
    assert is_allowed_host("my-tailnet:8765", "my-tailnet")
    assert is_allowed_host("my-tailnet:8765", ["other", "my-tailnet"])
    assert not is_allowed_host("my-tailnet:8765", "other")
    # Ports are stripped on both sides before comparing.
    assert is_allowed_host("my-tailnet:8765", "my-tailnet:9999")
