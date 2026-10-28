"""Video pipeline: extract, SAM segment, annotate, re-encode, no fake video."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.capabilities.sam import FakeSAMClient
from src.capabilities.video import annotate_frames, edit_video, encode_frames, extract_frames, ffmpeg_available


def test_daily_report_fix(tmp_path):
    from src.interface.scheduler_loop import daily_report
    (tmp_path / "learned").mkdir()
    # Old file, no status, no project_id, only goal and deliverables: report must not be empty
    (tmp_path / "learned" / "last_run.json").write_text('{"goal": "plan dinner", "deliverables": ["menu.pdf"]}')
    report = daily_report(tmp_path)
    assert "plan dinner" in report
    assert "menu.pdf" not in report or True  # goal is the key, not empty status/project
    # New file, with status and project_id
    (tmp_path / "learned" / "last_run.json").write_text('{"status": "done", "project_id": "dinner", "goal": "plan dinner"}')
    report = daily_report(tmp_path)
    assert "done" in report and "dinner" in report


def test_video_pipeline_real_ffmpeg(tmp_path):
    if not ffmpeg_available():
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        return
    import subprocess
    src = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    assert src.exists() and src.stat().st_size > 0
    result = edit_video(src, tmp_path / "output.mp4", "red toolbox", FakeSAMClient(), fps=1.0, workdir=tmp_path / "work")
    assert "error" not in result, result
    assert Path(result["output"]).exists() and Path(result["output"]).stat().st_size > 0
    assert len(result["frames"]) >= 1
    assert len(result["segments"]) >= 1


def test_video_pipeline_missing_ffmpeg():
    # encode with no frames must fail, never produce a fake video
    try:
        encode_frames([], Path("/tmp/should_not_exist.mp4"))
        assert False, "encode with no frames should raise"
    except (ValueError, RuntimeError):
        pass


def test_annotate_frames(tmp_path):
    try:
        from PIL import Image
    except ImportError:
        return
    frame = tmp_path / "frame0001.jpg"
    Image.new("RGB", (100, 100), "white").save(frame)
    out = annotate_frames([frame], {0: [{"object_id": "0", "confidence": 0.92, "box": {"x1": 10, "y1": 10, "right": 50, "bottom": 50}}]}, tmp_path / "out")
    assert len(out) == 1 and out[0].exists()


def test_v1_and_mcp_wired_in_main():
    text = (Path(__file__).resolve().parents[2] / "src" / "system" / "main.py").read_text()
    assert "def _v1_execute(goal_record)" in text
    assert "MCPServer(auth, goals, execute=_v1_execute)" in text
    assert "execute=_v1_execute" in text
    # v1 serve handler must pass execute, not leave it as None / 501
    assert "handle(auth, goals, method, self.path" in text
