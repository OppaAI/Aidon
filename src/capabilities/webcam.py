"""Webcam photo and video capture primitives.

The tool layer in ``system.main`` owns the mandatory human
approval gate. These functions only capture when called; they
never prompt, choose a device, or run automatically.

Webcam video capture: host-side, approval-gated at the
tool layer (the caller asks the human first; this module
only records). Mirrors the system-audio recorder's shape:
tries backends in order, never writes an empty file and
calls it a recording, raises with all backend errors when
nothing works.

Backends, in order:
  1. ffmpeg with v4l2: probes encoders first (fast) and
     picks libx264 when present, else mpeg4, then records
     one command for the requested duration.
  2. OpenCV VideoCapture: reads frames until the duration
     elapses, writes mp4v. Only used when ffmpeg is absent
     or its run fails.

Video only: no audio track. A talking-head clip with
subtitles needs an audio capture paired with it, which is
a separate tool call (record_system_audio), not this one.

Runs on the host, not in a sandbox: the camera is host
hardware, and no sandbox backend binds /dev/video0. That
is the same justification as the audio recorder. The
approval kind is "record" with force_ask: camera
activation is never pre-approved, never silent, and the
card names the duration and the output file before the
camera starts.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path


def _run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


def _ffmpeg_codec(runner, which) -> str | None:
    """libx264 when the ffmpeg build has it, else mpeg4,
    else None when ffmpeg is absent or the probe fails."""
    if which("ffmpeg") is None:
        return None
    try:
        proc = runner(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
    except Exception:
        return None
    text = (getattr(proc, "stdout", "") or "") + \
        (getattr(proc, "stderr", "") or "")
    if "libx264" in text:
        return "libx264"
    return "mpeg4"


def _record_ffmpeg(seconds: int, path: Path, device: str, runner,
                     which) -> None:
    codec = _ffmpeg_codec(runner, which)
    if codec is None:
        raise RuntimeError("ffmpeg not found or encoder probe failed")
    argv = ["ffmpeg", "-y", "-f", "v4l2", "-i", device,
            "-t", str(seconds), "-c:v", codec, "-pix_fmt", "yuv420p",
            str(path)]
    try:
        proc = runner(argv, timeout=seconds + 60)
    except Exception as exc:
        raise RuntimeError(
            f"ffmpeg: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0 or not path.is_file() or \
            path.stat().st_size == 0:
        detail = ((getattr(proc, "stderr", "") or "") or
                  (getattr(proc, "stdout", "") or "")).strip().splitlines()
        raise RuntimeError(
            f"ffmpeg: exit {proc.returncode}"
            + (f" ({detail[-1][:120]})" if detail else ""))


def _record_opencv(seconds: int, path: Path, device: str,
                   cv2_module=None) -> None:
    try:
        cv2 = cv2_module if cv2_module is not None else __import__("cv2")
    except ImportError as exc:
        raise RuntimeError("opencv not installed") from exc
    source: object = device
    if isinstance(device, str) and device.isdigit():
        source = int(device)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"camera not opened: {device}")
    writer = None
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("camera frame read failed")
            if writer is None:
                height = len(frame)
                width = len(frame[0]) if height else 0
                if not width or not height:
                    raise RuntimeError("camera returned an empty frame")
                fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                    float(fps), (int(width), int(height)))
                if not writer.isOpened():
                    raise RuntimeError("video writer not opened")
            writer.write(frame)
    finally:
        try:
            cap.release()
        except Exception:
            pass
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("opencv wrote no video")


def record_webcam_clip(seconds: int, out_path, device: str = "/dev/video0",
                       runner=None, cv2_module=None, which=None) -> str:
    """Record seconds of webcam video to out_path (mp4).
    Returns the output path. Raises ValueError on bad
    arguments, RuntimeError when no backend produces a
    non-empty file."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError("seconds must be an integer")
    if seconds < 1 or seconds > 600:
        raise ValueError("seconds must be 1..600")
    if not str(out_path).strip():
        raise ValueError("out_path is required")
    if not str(device or "").strip():
        raise ValueError("device is required")
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run = runner or _run
    find = which or shutil.which
    errors = []
    for name, fn in (("ffmpeg", lambda: _record_ffmpeg(
                            seconds, path, str(device), run, find)),
                     ("opencv", lambda: _record_opencv(
                            seconds, path, str(device), cv2_module))):
        try:
            fn()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        return str(path)
    raise RuntimeError("webcam recording failed: " + "; ".join(errors))


def capture_webcam_photo(out_path, device: str = "/dev/video0",
                         runner=None, cv2_module=None, which=None) -> str:
    """Capture exactly one still frame to ``out_path``.

    ffmpeg is preferred, with OpenCV as a fallback. The caller
    must enforce human approval before invoking this host-side
    camera operation.
    """
    if not str(out_path).strip():
        raise ValueError("out_path is required")
    if not str(device or "").strip():
        raise ValueError("device is required")
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run = runner or _run
    find = which or shutil.which
    errors = []
    if find("ffmpeg") is not None:
        try:
            proc = run(["ffmpeg", "-y", "-f", "v4l2", "-i", str(device),
                        "-frames:v", "1", "-update", "1", str(path)],
                       timeout=30)
            if (proc.returncode == 0 and path.is_file()
                    and path.stat().st_size > 0):
                return str(path)
            errors.append("ffmpeg did not produce an image")
        except Exception as exc:
            errors.append(f"ffmpeg: {type(exc).__name__}: {exc}")
    try:
        cv2 = cv2_module if cv2_module is not None else __import__("cv2")
        source: object = int(device) if str(device).isdigit() else str(device)
        cap = cv2.VideoCapture(source)
        try:
            if not cap.isOpened():
                raise RuntimeError(f"camera not opened: {device}")
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("camera frame read failed")
            if not cv2.imwrite(str(path), frame):
                raise RuntimeError("image writer failed")
        finally:
            cap.release()
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("opencv wrote no image")
        return str(path)
    except Exception as exc:
        errors.append(f"opencv: {type(exc).__name__}: {exc}")
    raise RuntimeError("webcam photo capture failed: " + "; ".join(errors))
