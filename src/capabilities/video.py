"""Video pipeline, so Aidon can edit his own demo video.

Pipeline, in order, no step faked:
1. extract_frames: ffmpeg extracts frames from a video,
   at a chosen fps, into a directory, as frame0001.jpg,
   frame0002.jpg, and so on. ffmpeg is an external
   tool, Aidon does not install it, if it is not on
   PATH the pipeline returns an error, never fake frames.
2. segment_frames: SAM segments each frame for a noun
   phrase, boxes and confidences, via src/capabilities/sam.py.
   SAM is the segment and track step, it does not
   decode masks here, mask payloads are opaque base85.
3. annotate_frames: PIL draws SAM boxes and labels
   on each frame, so the edited video visibly tracks
   the object. Pixel-mask compositing is NOT claimed:
   without the optional official meta-sam-parser
   package, masks cannot be decoded to pixels, so
   box overlay is the honest edited output. If a
   caller later adds mask decoding, that step replaces
   annotate, the rest of the pipeline stays the same.
4. encode_frames: ffmpeg re-encodes the annotated
   frames into a new video, libx264, yuv420p, at the
   same fps, so the result plays in a normal player.

edit_video runs all four steps, and returns a dict
with frames, segments, and output, or an error dict,
never a fake video file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(cmd: list, run=None):
    if run is not None:
        return run(cmd)
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def extract_frames(video_path, out_dir, fps: float = 1.0, run=None) -> list[Path]:
    """Extract frames with ffmpeg, return sorted frame paths, raise if ffmpeg is missing or fails."""
    if not ffmpeg_available() and run is None:
        raise RuntimeError("ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake frames are produced")
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "frame%04d.jpg")
    _run(["ffmpeg", "-y", "-i", str(video_path), "-vf", f"fps={fps}", pattern], run=run)
    frames = sorted(out_dir.glob("frame*.jpg"))
    if not frames and run is None:
        raise RuntimeError("ffmpeg extracted no frames, check the input video")
    return frames


def annotate_frames(frame_paths, segments_by_frame, out_dir) -> list[Path]:
    """Draw SAM boxes on frames with PIL, return annotated frame paths. Segments are a dict frame_index -> list of segment dicts."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("PIL is required to annotate video frames") from exc
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for index, frame_path in enumerate(frame_paths):
        image = Image.open(frame_path).convert("RGB")
        draw = ImageDraw.Draw(image)
        for segment in segments_by_frame.get(index, []):
            box = segment.get("box", {}) or {}
            if not box:
                continue
            x1, y1, x2, y2 = box.get("x1", 0), box.get("y1", 0), box.get("right", 0), box.get("bottom", 0)
            draw.rectangle([x1, y1, x2, y2], outline="#7de9ff", width=3)
            label = f"id {segment.get('object_id', '')} c={segment.get('confidence', '')}"
            draw.text((x1 + 2, y1 + 2), label, fill="#7de9ff")
        dest = out_dir / f"frame{index + 1:04d}.jpg"
        image.save(dest, "JPEG")
        out.append(dest)
    return out


def encode_frames(frame_paths, output_path, fps: float = 1.0, run=None, workdir=None) -> Path:
    """Re-encode frames into a video with ffmpeg, return output path, raise if ffmpeg is missing or fails."""
    if not ffmpeg_available() and run is None:
        raise RuntimeError("ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake video is produced")
    frame_paths = list(frame_paths)
    if not frame_paths:
        raise ValueError("encode_frames needs at least one frame")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if workdir is None:
        workdir = output_path.parent / "encode_frames_tmp"
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # ffmpeg image2 needs a numbered pattern, copy frames into that pattern, never invent frame content
    for index, frame_path in enumerate(frame_paths):
        shutil.copyfile(frame_path, workdir / f"frame{index + 1:04d}.jpg")
    pattern = str(workdir / "frame%04d.jpg")
    _run(["ffmpeg", "-y", "-framerate", str(fps), "-i", pattern, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output_path)], run=run)
    if not output_path.exists() and run is None:
        raise RuntimeError("ffmpeg did not produce an output video")
    return output_path


def segment_frames(frame_paths, prompt: str, sam_client) -> dict:
    """Segment each frame with SAM, return dict frame_index -> list of segment dicts, or an error dict if SAM fails."""
    segments_by_frame = {}
    for index, frame_path in enumerate(frame_paths):
        result = sam_client.segment([str(frame_path)], prompt)
        if "error" in result:
            return {"error": result["error"]}
        segments_by_frame[index] = result.get("segments", [])
    return segments_by_frame


def edit_video(input_video, output_video, prompt: str, sam_client, fps: float = 1.0, workdir=None) -> dict:
    """Full video edit pipeline: extract, SAM segment, annotate with boxes, re-encode. Returns frames, segments, output, or an error dict."""
    if not ffmpeg_available():
        return {"error": "ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake video is produced"}
    input_video = Path(input_video)
    output_video = Path(output_video)
    if workdir is None:
        workdir = output_video.parent / f"{output_video.stem}_work"
    workdir = Path(workdir)
    try:
        frames = extract_frames(input_video, workdir / "frames", fps=fps)
        segments = segment_frames(frames, prompt, sam_client)
        if "error" in segments:
            return {"error": segments["error"]}
        annotated = annotate_frames(frames, segments, workdir / "annotated")
        output = encode_frames(annotated, output_video, fps=fps, workdir=workdir / "encode_tmp")
    except Exception as exc:
        return {"error": f"video pipeline failed: {type(exc).__name__}: {exc}"}
    return {"input": str(input_video), "output": str(output), "prompt": prompt, "fps": fps,
            "frames": [str(f) for f in frames], "annotated": [str(f) for f in annotated],
            "segments": segments}
