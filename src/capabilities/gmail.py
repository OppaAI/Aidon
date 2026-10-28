"""Gmail send and read, via OAuth refresh token, stdlib only.

Email uses the direct Gmail API; the retired Google Workspace MCP
transport is not part of this implementation. The direct Gmail API is
the one that works from the keys the owner already holds:
GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN,
from the Aidon Gmail account, aidon.ai.agent@gmail.com,
scopes gmail.send and gmail.readonly. The refresh token is
exchanged for a short-lived access token, never stored in
a run record, never logged, and the access token is used
for one Gmail API call only.

If those keys are missing, email.py keeps drafts only, it
does not invent a send. If they are present, a sender
callable built here is the injected sender email.py uses,
so a draft can become a real send only after owner approval,
or after the owner-email exception, never by itself.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from email.message import EmailMessage


def refresh_access_token(client_id: str, client_secret: str,
                         refresh_token: str, post=None) -> str:
    """Exchange a refresh token for an access token, or raise."""
    post = post or _post_json
    data = post("https://oauth2.googleapis.com/token",
                {"client_id": client_id, "client_secret": client_secret,
                 "refresh_token": refresh_token, "grant_type": "refresh_token"})
    token = data.get("access_token", "")
    if not token:
        raise ValueError("Gmail refresh did not return an access token")
    return token


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def gmail_sender(client_id: str, client_secret: str, refresh_token: str,
                 post=None, send=None):
    """Return a sender(to, subject, body) callable, or raise on missing keys."""
    if not (client_id and client_secret and refresh_token):
        raise ValueError("Gmail OAuth keys are missing, email stays as drafts")

    def send_email(to: str, subject: str, body: str) -> dict:
        token = refresh_access_token(client_id, client_secret, refresh_token, post=post)
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        payload = {"raw": raw}
        if send is not None:
            return send(token, payload)
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())

    return send_email
