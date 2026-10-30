"""WebUI auth helper for remote access.

The WebUI is localhost only by default. This helper is the
check the parent applies before serving a WebUI request
when remote access is considered.

Rules, fail closed:
- If no token is set (None or empty), only localhost
  addresses are allowed: 127.0.0.1, any 127.* address,
  ::1, [::1], and localhost. Every other address is denied.
- If a token is set, every address, including localhost,
  must present headers with Authorization exactly equal
  to "Bearer <token>". A missing, wrong, or partial token
  is denied.
- Header keys are case-insensitive. Header values may be
  a string or a single-item list; only the first value is
  used, and it is stripped of surrounding whitespace.
- Nothing defaults to allowed: an unknown address, a
  missing header, or a missing token case returns False.
"""

from __future__ import annotations

from urllib.parse import urlparse

_LOCALHOSTS = {"127.0.0.1", "::1", "[::1]", "localhost"}


def _is_localhost(remote_addr: str) -> bool:
    addr = (remote_addr or "").strip().lower()
    if addr in _LOCALHOSTS:
        return True
    if addr.startswith("127."):
        return True
    return False


def _authorization(headers: dict) -> str | None:
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if str(key).lower() == "authorization":
            if isinstance(value, (list, tuple)):
                if not value:
                    return None
                value = value[0]
            return str(value).strip()
    return None


def is_authorized(headers: dict, token: str | None, remote_addr: str = "127.0.0.1") -> bool:
    """Return True only if the request is allowed, else False."""
    if token is None or not str(token).strip():
        return _is_localhost(remote_addr)
    expected = f"Bearer {token}"
    return _authorization(headers) == expected


def _host_name(host: str) -> str:
    """Hostname part of a Host header: lowercased, port stripped."""
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    if h.count(":") > 1:  # bare IPv6, no port to strip
        return h
    return h.split(":", 1)[0]


def is_allowed_host(host: str, allowed_hosts=None) -> bool:
    """True only if the Host is loopback or explicitly configured.

    An Origin check that compares Origin to Host is meaningless when
    the Host itself is attacker-controlled: a rebinding domain that
    resolves to 127.0.0.1 sends a matching Host and Origin pair. So
    the Host must first be loopback (127.*, ::1, localhost) or listed
    in AIDON_WEBUI_ALLOWED_HOSTS (comma-separated string or iterable
    of names). A missing Host is rejected.
    """
    name = _host_name(host)
    if not name:
        return False
    if _is_localhost(name):
        return True
    if allowed_hosts is None:
        return False
    if isinstance(allowed_hosts, str):
        allowed_hosts = [h.strip() for h in allowed_hosts.split(",")]
    return any(name == _host_name(a) for a in allowed_hosts if a)


def is_allowed_origin(headers: dict, host: str) -> bool:
    """Same-origin check for browser POSTs to the WebUI.

    A missing Origin header (curl, non-browser clients) is allowed:
    those callers still pass the localhost/token auth check. A present
    Origin must match the request Host exactly (host and port); a
    foreign Origin is a cross-site POST and is rejected. Origin
    "null" (sandboxed iframe, file URL) is rejected: the WebUI page
    is never served from such a context.
    """
    origin = None
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "origin":
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else None
                origin = str(value).strip() if value is not None else None
                break
    if origin is None or origin == "":
        return True
    netloc = urlparse(origin).netloc
    if not netloc:
        return False
    return netloc.lower() == (host or "").strip().lower()
