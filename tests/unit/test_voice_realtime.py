"""Tests for streaming Muse Voice Transcribe (realtime WebSocket).

A fake Meta server runs over a socketpair: it answers the HTTP
upgrade, checks the handshake frame, emits partial and final
transcript events, and closes. No network, no key.
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import time

from src.capabilities.voice import RealtimeTranscriber, _ws_accept


def _read_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _read_frame(sock):
    first, second = _read_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _server_frame(opcode, payload):
    header = bytes([0x80 | opcode])
    if len(payload) < 126:
        header += bytes([len(payload)])
    else:
        header += bytes([126]) + struct.pack(">H", len(payload))
    return header + payload


def _fake_meta_server(server_sock, seen, wrong_accept=False):
    """Behave like the realtime endpoint for one session."""
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = server_sock.recv(1)
            if not chunk:
                return
            data += chunk
        key = ""
        for line in data.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = "wrong" if wrong_accept else _ws_accept(key)
        server_sock.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        while True:
            opcode, payload = _read_frame(server_sock)
            if opcode == 0x8:
                return
            if opcode == 0x1:
                msg = json.loads(payload.decode("utf-8"))
                if "authorization" in msg:
                    seen["handshake"] = msg
                    server_sock.sendall(_server_frame(
                        0x1, json.dumps({"sessionId": "test-session"}).encode()))
                elif msg.get("type") == "endStream":
                    seen["ended"] = True
                    server_sock.sendall(_server_frame(
                        0x1, json.dumps({"type": "transcript",
                                         "transcript": "Hello world.",
                                         "final": True,
                                         "audioProcessedMs": 2000}).encode()))
                    server_sock.sendall(_server_frame(0x8, b""))
                    return
            elif opcode == 0x2:
                seen.setdefault("audio", []).append(payload)
                server_sock.sendall(_server_frame(
                    0x1, json.dumps({"type": "transcript",
                                     "transcript": "hello",
                                     "final": False,
                                     "audioProcessedMs": 1000}).encode()))
    except Exception:
        return


def _wait_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return fn()


def test_realtime_stream_partial_then_final():
    client_sock, server_sock = socket.socketpair()
    seen = {}
    thread = threading.Thread(target=_fake_meta_server,
                              args=(server_sock, seen), daemon=True)
    thread.start()
    rt = RealtimeTranscriber("test-key", session_id="test-session",
                             socket_factory=lambda h, p: client_sock)
    try:
        assert rt.connect(timeout=5) == "test-session"
        assert _wait_until(lambda: "handshake" in seen)
        hs = seen["handshake"]
        assert hs["authorization"] == {"accessToken": "Bearer test-key"}
        assert hs["audioEncoding"] == "PCM_24KHZ"
        assert hs["mode"] == "PUSH_TO_TALK"
        assert hs["partialMode"] == "CUMULATIVE"
        assert rt.send_audio(b"\x00\x01" * 100) is True
        assert _wait_until(lambda: rt.transcript == "hello")
        assert rt.final is False
        result = rt.end(timeout=5)
        assert result["transcript"] == "Hello world."
        assert result["final"] is True
        assert seen.get("ended") is True
    finally:
        rt.close()
        client_sock.close()
        server_sock.close()


def test_realtime_accept_mismatch_fails_connect():
    client_sock, server_sock = socket.socketpair()
    thread = threading.Thread(target=_fake_meta_server,
                              args=(server_sock, {}), daemon=True,
                              kwargs={"wrong_accept": True})
    thread.start()
    rt = RealtimeTranscriber("test-key", session_id="test-session",
                             socket_factory=lambda h, p: client_sock)
    try:
        import pytest
        with pytest.raises(ConnectionError):
            rt.connect(timeout=5)
    finally:
        rt.close()
        client_sock.close()
        server_sock.close()


def test_realtime_send_after_close_is_false():
    rt = RealtimeTranscriber("test-key", session_id="x")
    rt.close()
    assert rt.send_audio(b"\x00\x01") is False
