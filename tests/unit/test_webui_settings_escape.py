"""Settings-page API-key rows escape owner-supplied key names."""

from src.interface.webui import _api_key_row


def test_api_key_row_escapes_name_scope_and_prefix():
    row = _api_key_row({
        "name": '<script>alert("x")</script>',
        "scope": 'a"&b',
        "key_hash_prefix": "ab<cd",
    })
    assert "<script>" not in row
    assert "&lt;script&gt;" in row
    assert "&quot;" in row
    assert "ab&lt;cd" in row


def test_api_key_row_keeps_plain_values_readable():
    row = _api_key_row({"name": "owner", "scope": "owner", "key_hash_prefix": "abc123"})
    assert "<label>owner</label>" in row
    assert "hash abc123" in row
