"""Origin check for WebUI browser POSTs (CSRF guard)."""

from src.interface.webui_auth import is_allowed_origin


def test_missing_origin_allowed_for_non_browser_clients():
    assert is_allowed_origin({}, "127.0.0.1:8765") is True


def test_same_origin_allowed():
    headers = {"Origin": "http://127.0.0.1:8765"}
    assert is_allowed_origin(headers, "127.0.0.1:8765") is True


def test_same_origin_case_insensitive():
    headers = {"Origin": "http://LOCALHOST:8765"}
    assert is_allowed_origin(headers, "localhost:8765") is True


def test_foreign_origin_rejected():
    headers = {"Origin": "https://evil.example.com"}
    assert is_allowed_origin(headers, "127.0.0.1:8765") is False


def test_same_host_wrong_port_rejected():
    headers = {"Origin": "http://127.0.0.1:9999"}
    assert is_allowed_origin(headers, "127.0.0.1:8765") is False


def test_null_origin_rejected():
    assert is_allowed_origin({"Origin": "null"}, "127.0.0.1:8765") is False


def test_list_valued_origin_header_uses_first():
    headers = {"Origin": ["http://127.0.0.1:8765"]}
    assert is_allowed_origin(headers, "127.0.0.1:8765") is True
