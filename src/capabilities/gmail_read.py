"""Gmail inbox read, via OAuth refresh token, stdlib only.

Send is separate, in gmail.py. This module is read only,
with the gmail.readonly scope, and it never sends. It
exchanges a refresh token for a short-lived access token,
using the same refresh helper as the sender, lists recent
messages, then fetches each message's metadata: subject,
sender, snippet, and date. The access token is used for
one read call sequence only, never stored in a run record,
and never logged.

Function read_inbox(client_id, client_secret, refresh_token,
post=None, get=None, max_results=10):
- post is injectable, following the gmail.py convention:
  post(url, payload_dict) -> dict, used for the token
  exchange. get is injectable: get(url, headers_dict)
  -> dict, used for the list and message calls.
- Real calls, when post/get are not injected, use
  urllib with a 15 second timeout, and the Gmail API
  v1 endpoints under users/me/messages.
- Message metadata is read from payload.headers, with
  names Subject, From, and Date, and from the snippet
  field. A response that already carries subject, sender,
  or date at the top level is also accepted, so a test
  fake does not need to build full Gmail payloads.
- Raises on a missing refresh token, or missing client
  id or secret, on a token exchange with no access token,
  and on an empty list response, meaning a response that
  is not a dict at all. An inbox with zero messages,
  a dict with an empty messages list, is a valid empty
  result, and returns an empty list, not an error.

Limits: this reads metadata and snippets only, not full
message bodies or attachments. A full body read is a
separate function, not added here, because metadata is
enough for triage, and bodies can contain sensitive data
that should not be copied into a run record by default.
"""

from __future__ import annotations

import json
import urllib.request

from .gmail import refresh_access_token

_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def _headers_from_payload(message: dict) -> dict:
    headers: dict[str, str] = {}
    payload = message.get("payload", {}) if isinstance(message, dict) else {}
    for header in payload.get("headers", []) if isinstance(payload, dict) else []:
        if isinstance(header, dict) and header.get("name"):
            headers[str(header["name"]).lower()] = str(header.get("value", ""))
    return headers


def read_inbox(client_id: str, client_secret: str, refresh_token: str,
               post=None, get=None, max_results: int = 10) -> list[dict]:
    """Read recent inbox metadata, or raise on missing keys or empty response."""
    if not (client_id and client_secret and refresh_token):
        raise ValueError("Gmail OAuth keys are missing, inbox read is refused")
    token = refresh_access_token(client_id, client_secret, refresh_token, post=post)
    get_fn = get or _get_json
    headers = {"Authorization": f"Bearer {token}"}
    listing = get_fn(f"{_LIST_URL}?maxResults={max_results}", headers)
    if not isinstance(listing, dict):
        raise ValueError("Gmail message list returned an empty response")
    entries = listing.get("messages", []) or []
    results: list[dict] = []
    for entry in entries:
        message_id = entry.get("id", "") if isinstance(entry, dict) else ""
        if not message_id:
            continue
        message = get_fn(f"{_LIST_URL}/{message_id}?format=metadata", headers)
        if not isinstance(message, dict):
            raise ValueError("Gmail message read returned an empty response")
        header_map = _headers_from_payload(message)
        results.append({
            "id": message.get("id", message_id),
            "subject": header_map.get("subject", message.get("subject", "")),
            "sender": header_map.get("from", message.get("sender", message.get("from", ""))),
            "snippet": message.get("snippet", ""),
            "date": header_map.get("date", message.get("date", "")),
        })
    return results
