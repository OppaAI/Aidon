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
import os
import shutil
import subprocess
import tempfile
import uuid
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
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {"ok": False,
                    "error": "ffmpeg is required to convert browser audio to mono 24 kHz WAV",
                    "text": ""}
        suffix = ".wav" if (content_type or "").lower().startswith("audio/wav") else ".webm"
        with tempfile.TemporaryDirectory(prefix="aidon-asr-") as work:
            source = os.path.join(work, "input" + suffix)
            target = os.path.join(work, "audio.wav")
            with open(source, "wb") as handle:
                handle.write(audio_bytes)
            converted = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-i", source,
                 "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
                 "-map_metadata", "-1", target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                check=False,
            )
            if converted.returncode != 0 or not os.path.isfile(target):
                detail = converted.stderr.decode("utf-8", "replace")[-300:].strip()
                return {"ok": False, "error": "audio conversion failed" + (f": {detail}" if detail else ""), "text": ""}
            with open(target, "rb") as handle:
                wav = handle.read()
        boundary = "----AidonASR" + uuid.uuid4().hex
        request_json = json.dumps({
            "model": "muse-voice-transcribe-1.0",
            "audioEncoding": "WAV",
            "mode": "PUSH_TO_TALK",
        })
        chunks = []
        def field(name, value, mime):
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                value if isinstance(value, bytes) else value.encode(),
                b"\r\n",
            ])
        field("request", request_json, "application/json")
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="audio"; filename="recording.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n", wav, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            "https://api.meta.ai/v1/asr/transcribe", data=b"".join(chunks), method="POST",
            headers={"Authorization": f"Bearer {transcribe_key}",
                     "Accept": "application/json",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
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
