"""Voice, Aidon: Fish Audio TTS, and Muse Voice Transcribe.

Old keys, only: AIDON_FISH_API_KEY, AIDON_TTS_VOICE_ID,
AIDON_TRANSCRIBE_API_KEY. No new duplicate.
TTS is Fish Audio S2.1 Pro, same as Aidon, free tier
through 2026-11-30 in Aidon, no SLA, check the current
terms before a demo. A missing Fish key is an error, never
a silent fallback to another voice.
Transcribe is Muse Voice Transcribe, via the Meta Model
API, with AIDON_TRANSCRIBE_API_KEY, falling back to
AIDON_API_KEY when the transcribe key is empty, as in
Aidon. A missing key, and a missing audio file, are
errors, never invented text.
Post functions are injected for tests, production uses
urllib, and the API error, never the key, is returned.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error


def tts(text: str, fish_key: str, voice_id: str = "", post=None) -> dict:
    if not fish_key:
        return {"ok": False, "error": "AIDON_FISH_API_KEY not set", "audio": b""}
    if not text.strip():
        return {"ok": False, "error": "empty text", "audio": b""}
    payload = {"text": text, "format": "mp3"}
    if voice_id:
        payload["reference_id"] = voice_id
    try:
        if post is not None:
            audio = post("https://api.fish.audio/v1/tts", fish_key, payload)
            return {"ok": True, "audio": audio}
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            "https://api.fish.audio/v1/tts", data=data, method="POST",
            headers={"Authorization": f"Bearer {fish_key}",
                     "model": "s2.1-pro-free",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"ok": True, "audio": response.read()}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "audio": b""}


def open_tts_stream(text: str, fish_key: str, voice_id: str = ""):
    """Open Fish TTS and return its response for progressive reads.

    The caller owns the returned response and must close it. Keeping the
    response open lets the WebUI forward audio chunks as Fish produces them,
    instead of waiting for ``response.read()`` to collect the full MP3.
    """
    if not fish_key:
        raise ValueError("AIDON_FISH_API_KEY not set")
    if not text.strip():
        raise ValueError("empty text")
    payload = {"text": text, "format": "mp3"}
    if voice_id:
        payload["reference_id"] = voice_id
    request = urllib.request.Request(
        "https://api.fish.audio/v1/tts", data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {fish_key}",
                 "model": "s2.1-pro-free",
                 "Content-Type": "application/json"})
    return urllib.request.urlopen(request, timeout=60)


def transcribe(audio_bytes: bytes, transcribe_key: str, post=None,
               content_type: str = "audio/wav") -> dict:
    if not transcribe_key:
        return {"ok": False, "error": "AIDON_TRANSCRIBE_API_KEY not set", "text": ""}
    if not audio_bytes:
        return {"ok": False, "error": "empty audio", "text": ""}
    try:
        if post is not None:
            result = post("https://api.meta.ai/v1/asr/transcribe", transcribe_key, audio_bytes)
            return {"ok": True, "text": result.get("text", "") if isinstance(result, dict) else str(result)}
        request = urllib.request.Request(
            "https://api.meta.ai/v1/asr/transcribe", data=audio_bytes, method="POST",
            headers={"Authorization": f"Bearer {transcribe_key}",
                     "Content-Type": content_type or "audio/wav"})
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
            return {"ok": True, "text": result.get("text", "")}
    except urllib.error.HTTPError as exc:
        # Preserve the provider's safe diagnostic so the WebUI can explain
        # an unsupported recording format or rejected credential instead of
        # reducing every failure to "HTTP error".
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return {"ok": False, "error": f"transcription provider HTTP {exc.code}{suffix}", "text": ""}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "text": ""}
