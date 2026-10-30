"""Webcam capture: validation, ffmpeg codec choice, backend
fallback, and the approval-gated tool closure."""

from types import SimpleNamespace

import pytest

from src.capabilities import webcam as webcam_mod
from src.capabilities.webcam import record_webcam_clip
from src.system.main import _goal_extra_tools


def test_validation(tmp_path):
    with pytest.raises(ValueError, match="1..600"):
        record_webcam_clip(0, tmp_path / "c.mp4")
    with pytest.raises(ValueError, match="integer"):
        record_webcam_clip("30", tmp_path / "c.mp4")
    with pytest.raises(ValueError, match="out_path"):
        record_webcam_clip(30, "")
    with pytest.raises(ValueError, match="device"):
        record_webcam_clip(30, tmp_path / "c.mp4", device="")


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ffmpeg_runner(encoders_stdout, calls):
    def run(argv, timeout):
        calls.append(list(argv))
        if argv[1:3] == ["-hide_banner", "-encoders"]:
            return _Proc(stdout=encoders_stdout)
        out_path = argv[-1]
        with open(out_path, "wb") as fh:
            fh.write(b"fakevideo")
        return _Proc()
    return run


def _which_present(name):
    return "/usr/bin/ffmpeg" if name == "ffmpeg" else None


def test_ffmpeg_libx264_and_mpeg4_fallback(tmp_path):
    calls = []
    out = tmp_path / "clip.mp4"
    result = record_webcam_clip(
        30, out, runner=_ffmpeg_runner(" V libx264 encoder", calls),
        which=_which_present)
    assert result == str(out) and out.stat().st_size > 0
    record_argv = calls[1]
    assert record_argv[1:4] == ["-y", "-f", "v4l2"]
    assert "libx264" in record_argv and "30" in record_argv
    calls2 = []
    out2 = tmp_path / "clip2.mp4"
    record_webcam_clip(
        30, out2, runner=_ffmpeg_runner(" V mpeg4 encoder", calls2),
        which=_which_present)
    assert "mpeg4" in calls2[1] and "libx264" not in calls2[1]


class _FakeFrame(list):
    pass


class _FakeCapture:
    def __init__(self, source):
        self.source = source

    def isOpened(self):
        return True

    def get(self, prop):
        return 20.0

    def read(self):
        frame = _FakeFrame([_FakeFrame([0] * 8) for _ in range(6)])
        return True, frame

    def release(self):
        pass


class _FakeWriter:
    def __init__(self, path, fourcc, fps, size):
        self.path = path
        self.writes = 0

    def isOpened(self):
        return True

    def write(self, frame):
        self.writes += 1

    def release(self):
        with open(self.path, "wb") as fh:
            fh.write(b"fakevideo")


class _FakeCv2:
    CAP_PROP_FPS = 5

    def __init__(self):
        self.writer = None

    def VideoCapture(self, source):
        return _FakeCapture(source)

    def VideoWriter(self, path, fourcc, fps, size):
        self.writer = _FakeWriter(path, fourcc, fps, size)
        return self.writer

    def VideoWriter_fourcc(self, *chars):
        return 0


def test_opencv_fallback_when_ffmpeg_absent(tmp_path):
    fake_cv2 = _FakeCv2()
    out = tmp_path / "clip.mp4"
    result = record_webcam_clip(
        1, out, runner=lambda argv, timeout: (_ for _ in ()).throw(
            AssertionError("ffmpeg must not run")),
        cv2_module=fake_cv2, which=lambda name: None)
    assert result == str(out) and out.stat().st_size > 0
    assert fake_cv2.writer.writes >= 1


def test_all_backends_failing_reports_both(tmp_path):
    class _ClosedCapture:
        def isOpened(self):
            return False

    class _NoCam:
        CAP_PROP_FPS = 5

        def VideoCapture(self, source):
            return _ClosedCapture()

        def VideoWriter(self, *args):
            raise AssertionError("must not reach writer")

        def VideoWriter_fourcc(self, *chars):
            return 0

    with pytest.raises(RuntimeError) as exc:
        record_webcam_clip(30, tmp_path / "c.mp4",
                           runner=lambda argv, timeout: _Proc(
                               returncode=1, stderr="no device"),
                           cv2_module=_NoCam(), which=lambda name: None)
    assert "ffmpeg" in str(exc.value) and "opencv" in str(exc.value)


class _Gate:
    def __init__(self, verdict="approved"):
        self.verdict = verdict
        self.seen = []

    def ask(self, kind, title, body, action_class=None, force_ask=False):
        self.seen.append((kind, title, body, force_ask))
        return SimpleNamespace(verdict=self.verdict)


def test_closure_approval_and_rejection(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        webcam_mod, "record_webcam_clip",
        lambda seconds, out, device: (calls.append((seconds, out, device)),
                                       str(out))[1])
    gate = _Gate()
    tools = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                              gate=gate)
    result = tools["record_webcam"](30, "clip.mp4")
    assert result == str(tmp_path / "clip.mp4")
    assert calls == [(30, str(tmp_path / "clip.mp4"), "/dev/video0")]
    kind, title, body, force_ask = gate.seen[-1]
    assert kind == "record" and title == "Record webcam video"
    assert "30" in body and force_ask is True
    gate2 = _Gate(verdict="rejected")
    tools2 = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                               gate=gate2)
    assert tools2["record_webcam"](30, "clip.mp4").startswith(
        "webcam recording rejected")
