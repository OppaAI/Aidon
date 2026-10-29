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
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
import urllib.request
import urllib.error


def correct_asr_text(text: str) -> str:
    """Correct a high-confidence speech confusable in web-lookup requests.

    The speech service can hear ``check online`` as ``chat online``.  Do not
    rewrite every occurrence: ``chat online`` is also a legitimate request.
    The correction is applied only when the surrounding utterance clearly
    asks for verification, eligibility, current rules, or another lookup.
    """
    value = str(text or "").strip()
    lowered = value.lower()
    lookup_context = (
        "see if", "qualif", "verify", "whether", "eligib", "current",
        "rules", "look up", "research", "check", "latest",
    )
    if "chat online" not in lowered or not any(marker in lowered for marker in lookup_context):
        return value

    # Replace the ASR-confused phrase while retaining the user's casing.
    return re.sub(r"\bchat online\b", lambda match: (
        "Check online" if match.group(0)[0].isupper() else "check online"
    ), value, flags=re.IGNORECASE)


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
            raw_text = result.get("text", "") if isinstance(result, dict) else str(result)
            return {"ok": True, "text": correct_asr_text(raw_text)}
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {"ok": False,
                    "error": "ffmpeg is required to convert browser audio to mono 24 kHz WAV",
                    "text": ""}
        lowered_type = (content_type or "").lower()
        suffix = ".wav" if lowered_type.startswith("audio/wav") else ".mp4" if lowered_type.startswith("audio/mp4") else ".webm"
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
            text = result.get("text") or result.get("transcript") or ""
            if not text and isinstance(result.get("turns"), list):
                text = " ".join(str(turn.get("text", "")) for turn in result["turns"]
                                if isinstance(turn, dict)).strip()
            return {"ok": True, "text": correct_asr_text(text)}
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


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_HOST = "api.meta.ai"
_WS_PATH = "/v1/asr/realtime"


def _ws_accept(key: str) -> str:
    import base64 as _b64
    import hashlib as _hashlib
    digest = _hashlib.sha1((key + _WS_GUID).encode()).digest()
    return _b64.b64encode(digest).decode()


def _ws_frame(opcode: int, payload: bytes) -> bytes:
    """One masked client-to-server WebSocket frame."""
    import os as _os
    import struct as _struct
    mask = _os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + _struct.pack(">H", length)
    else:
        header += bytes([0x80 | 127]) + _struct.pack(">Q", length)
    return header + mask + masked


class RealtimeTranscriber:
    """Streaming Muse Voice Transcribe over its realtime WebSocket.

    Stdlib only: raw socket + TLS, WebSocket handshake and frames
    by hand, so no new dependency. One instance is one Meta
    session. The browser never sees the key: the WebUI server
    holds this object, the browser only sends PCM chunks and
    reads partials back over its own HTTP calls.

    Protocol (Meta speech-to-text docs): connect to
    wss://api.meta.ai/v1/asr/realtime, handshake as the first
    JSON text frame (authorization lives in the frame, not the
    HTTP header), then raw PCM int16 mono 24 kHz binary frames.
    partialMode CUMULATIVE means each transcript event carries
    the complete current hypothesis: replace, never append.
    In PUSH_TO_TALK mode the turn ends with a transcript event
    carrying final=true, after the client sends endStream.
    """

    def __init__(self, key: str, session_id: str | None = None,
                 socket_factory=None):
        self.key = key
        self.session_id = session_id or uuid.uuid4().hex
        self._socket_factory = socket_factory
        self._sock = None
        self._send_lock = threading.Lock()
        self._done = threading.Event()
        self._ack = threading.Event()
        self._transcript = ""
        self._final = False
        self._error = ""
        self._recv_thread = None
        self._closed = False

    @property
    def transcript(self) -> str:
        return self._transcript

    @property
    def final(self) -> bool:
        return self._final

    @property
    def error(self) -> str:
        return self._error

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("websocket closed")
            data += chunk
        return data

    def _read_headers(self) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(1)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            data += chunk
            if len(data) > 65536:
                raise ValueError("websocket handshake too large")
        return data.decode("utf-8", "replace")

    def connect(self, timeout: float = 15.0) -> str:
        """Connect, handshake, and wait for the session acknowledgement."""
        import base64 as _b64
        import socket as _socket
        import ssl as _ssl
        if self._socket_factory is not None:
            sock = self._socket_factory(_WS_HOST, 443)
        else:
            sock = _socket.create_connection((_WS_HOST, 443), timeout=timeout)
            sock = _ssl.create_default_context().wrap_socket(
                sock, server_hostname=_WS_HOST)
        self._sock = sock
        self._sock.settimeout(timeout)
        ws_key = _b64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {_WS_PATH}?sessionId={self.session_id} HTTP/1.1\r\n"
            f"Host: {_WS_HOST}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        headers = self._read_headers()
        lines = headers.split("\r\n")
        if " 101 " not in lines[0]:
            raise ConnectionError(f"websocket handshake failed: {lines[0]}")
        accept = ""
        for line in lines[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        if accept != _ws_accept(ws_key):
            raise ConnectionError("websocket accept key mismatch")
        self._send_text(json.dumps({
            "authorization": {"accessToken": f"Bearer {self.key}"},
            "audioEncoding": "PCM_24KHZ",
            "model": "muse-voice-transcribe-1.0",
            "mode": "PUSH_TO_TALK",
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
        }))
        # The connect timeout must not survive into the session: the
        # recv loop's job is to wait for server frames, and any idle
        # gap longer than the timeout (a pause before speaking, a
        # silent mic) would otherwise end the loop, after which
        # every audio chunk fails with 410. Clear it BEFORE the recv
        # thread starts: a recv already waiting under the old
        # timeout still fires even after another thread clears it.
        # close() unblocks the blocking recv via shutdown, so
        # nothing hangs on teardown.
        self._sock.settimeout(None)
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             daemon=True)
        self._recv_thread.start()
        if not self._ack.wait(timeout):
            raise TimeoutError("realtime session acknowledgement timed out")
        return self.session_id

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._sock is None:
                raise ConnectionError("websocket not connected")
            self._sock.sendall(_ws_frame(opcode, payload))

    def _send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _read_frame(self):
        """Read one frame; returns (opcode, payload, fin)."""
        import struct as _struct
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = _struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = _struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[i % 4]
                            for i, byte in enumerate(payload))
        return opcode, payload, fin

    def _recv_loop(self) -> None:
        frag_opcode = None
        frag_data = b""
        try:
            while True:
                opcode, payload, fin = self._read_frame()
                if opcode == 0x8:
                    # A server close used to be silent too: the next
                    # audio chunk failed with a bare 410 and nothing
                    # said Meta ended the session. Keep the reason —
                    # unless the final transcript already arrived, in
                    # which case the close is the normal session end
                    # (e.g. "No more transcript segments") and must
                    # not surface as an error banner on a success.
                    if not self._error and not self._final:
                        reason = ""
                        if len(payload) > 2:
                            reason = payload[2:].decode("utf-8", "replace")
                        self._error = ("server closed the session: " + reason
                                       if reason else "server closed the session")
                    try:
                        self._send_frame(0x8, b"")
                    except Exception:
                        pass
                    break
                if opcode == 0x9:
                    try:
                        self._send_frame(0xA, payload)
                    except Exception:
                        pass
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x0 and frag_opcode is not None:
                    frag_data += payload
                    if not fin:
                        continue
                    opcode, payload = frag_opcode, frag_data
                    frag_opcode, frag_data = None, b""
                elif not fin and opcode in (0x1, 0x2):
                    frag_opcode, frag_data = opcode, payload
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if "type" not in msg and "sessionId" in msg:
                    self._ack.set()
                    continue
                if msg.get("type") == "transcript":
                    self._transcript = str(msg.get("transcript", ""))
                    if msg.get("final"):
                        self._final = True
                elif msg.get("type") == "error":
                    self._error = str(msg.get("message", "transcribe error"))
        except Exception as exc:
            # A dead recv loop used to be silent: the next audio
            # chunk failed with 410 and nothing said why. Keep the
            # reason so the failure is diagnosable.
            if not self._error:
                self._error = str(exc)
        finally:
            self._done.set()

    def send_audio(self, pcm: bytes) -> bool:
        """Send one raw PCM int16 mono 24 kHz chunk. False when closed."""
        if not pcm or self._done.is_set() or self._sock is None:
            return False
        try:
            self._send_frame(0x2, pcm)
            return True
        except Exception:
            return False

    def end(self, timeout: float = 30.0) -> dict:
        """End input and wait for the final transcript or close."""
        try:
            self._send_text(json.dumps({"type": "endStream"}))
        except Exception:
            pass
        self._done.wait(timeout)
        return {"transcript": correct_asr_text(self._transcript),
                "final": self._final, "error": self._error}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                self._sock.close()
        except Exception:
            pass
        self._done.set()
