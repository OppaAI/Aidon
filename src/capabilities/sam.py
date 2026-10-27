"""SAM 3.1 segmentation, for video editing, not for chat vision.

What SAM is for: given an image, or a sequence of video
frames, plus a noun phrase like "red toolbox", or a box
or points, SAM returns pixel-precise segmentation masks,
boxes, and confidences, and it tracks the same object
across frames, identity-preserving. That is what video
editing needs: cut out a subject, keep it across frames,
replace a background, or track an object while the
camera moves. Spark vision answers "what is in this
image", it does not give you pixels to cut with, so
Spark vision alone cannot edit a video, SAM can.

What SAM is not: a video editor by itself. The pipeline
is: extract frames, SAM segment and track, composite
with the masks, re-encode the video. This module is
the segment and track step only. Frame extraction and
re-encoding need ffmpeg, which is not built here, and
is not a SAM feature, so a caller that wants a finished
edited video still needs those steps, and must say so.

API shape, from the official meta-sam protocol:
POST <base>/responses, Responses API, model id from
AIDON_SAM_MODEL, default "meta-sam", verify against
the Meta Model API console, because the exact model
id is not guaranteed by this code. Input is a user
message with input_text, the noun phrase or object
prompts, plus one input_image per frame, as a data
URL. Output is special-token text, one line per frame,
with box and mask tokens. Mask payloads are base85
and are kept opaque here, decoding needs the official
meta-sam-parser package, which is optional, and is
only used for an annotated preview, never for the
agent's reasoning, which uses boxes, confidences,
and mask size and encoding.

Safety: SAM needs a Meta Model API key, same endpoint
and key as Spark by default, AIDON_SAM_MODEL only
for the model name, no separate key required unless
you set one. A segment result never contains a secret,
and mask payloads are not stored in run records by
default, only boxes, confidences, and mask metadata.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MODEL_DEFAULT = "meta-sam"
_FRAME_RE = re.compile(r"<(\d+)f\s*(?:;[^>]*)?>")
_TOKEN_RE = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|]*))?\|>")


def _fields(body: str | None) -> dict:
    out = {}
    for part in (body or "").split(";"):
        part = part.strip()
        if part and "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


@dataclass
class Segment:
    object_id: str
    frame: int
    box: dict
    confidence: float | None
    mask_h: int
    mask_w: int
    mask_encoding: str
    mask_payload: str = ""


def parse_sam_output(text: str) -> list[Segment]:
    """Parse SAM wire text into Segment records, skip malformed lines, never crash."""
    segments = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        frame_match = _FRAME_RE.match(line)
        if not frame_match:
            continue
        frame = int(frame_match.group(1))
        rest = line[frame_match.end():]
        parts = re.split(r",(?=\d+<\|)", rest)
        for part in parts:
            part = part.strip()
            id_match = re.match(r"(\d+)(<\|.*)", part, re.S)
            if not id_match:
                continue
            object_id, tokens_text = id_match.groups()
            box = {}
            confidence = None
            mask_h = mask_w = 0
            mask_encoding = ""
            mask_payload = ""
            for token_name, token_body in _TOKEN_RE.findall(tokens_text):
                fields = _fields(token_body)
                if token_name == "box":
                    try:
                        box = {"x1": float(fields.get("x1", 0)), "y1": float(fields.get("y1", 0)),
                               "right": float(fields.get("x2", 0)), "bottom": float(fields.get("y2", 0)),
                               "w": float(fields.get("w", 0)), "h": float(fields.get("h", 0))}
                    except ValueError:
                        box = {}
                    if "c" in fields:
                        try:
                            confidence = float(fields["c"])
                        except ValueError:
                            pass
                elif token_name == "mask":
                    data = fields.get("data", "")
                    if "," in data:
                        size_part, payload = data.split(",", 1)
                        # data format is H,W,payload in old wire, or payload only in some lanes
                        size_bits = size_part.split(",")
                        try:
                            mask_h = int(fields.get("h", 0)) or 0
                            mask_w = int(fields.get("w", 0)) or 0
                        except ValueError:
                            pass
                        mask_payload = payload
                        mask_encoding = "lossless" if payload.startswith("~") else "one_bit"
            segments.append(Segment(object_id=object_id, frame=frame, box=box,
                                    confidence=confidence, mask_h=mask_h, mask_w=mask_w,
                                    mask_encoding=mask_encoding, mask_payload=mask_payload))
    return segments


def _segment_dict(segment: Segment) -> dict:
    data = {"object_id": segment.object_id, "frame": segment.frame, "box": segment.box,
            "confidence": segment.confidence, "mask_h": segment.mask_h, "mask_w": segment.mask_w,
            "mask_encoding": segment.mask_encoding}
    return data


def _extract_output_text(data: dict) -> str | None:
    for item in data.get("output", []) or []:
        for part in item.get("content", []) or []:
            if part.get("type") == "output_text" and part.get("text"):
                return part["text"]
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    return None


def build_object_prompt(noun_phrase: str, boxes: list[dict] | None = None) -> str:
    """Build the SAM input text: a noun phrase, plus optional box prompts."""
    text = noun_phrase.strip()
    if boxes:
        for box in boxes:
            text += f" box[{box.get('x1', 0)},{box.get('y1', 0)},{box.get('x2', 0)},{box.get('y2', 0)}]"
    return text


class SAMClient:
    def __init__(self, base_url: str, api_key: str, model: str = MODEL_DEFAULT, post=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._post = post

    def segment(self, images: list, input_text: str, propagation: str = "both", include_confidence: bool = True) -> dict:
        """Segment one image, or track across a list of video frames. Returns segments or an error dict, never raises on API failure."""
        if not self.api_key:
            return {"error": "SAM unavailable: no Meta Model API key"}
        if not images:
            return {"error": "SAM needs at least one image or frame"}
        content = [{"type": "input_text", "text": input_text}]
        for image in images:
            if isinstance(image, (str, Path)) and Path(image).exists():
                data = Path(image).read_bytes()
                url = f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
            elif isinstance(image, bytes):
                url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"
            else:
                url = str(image)
            content.append({"type": "input_image", "image_url": url})
        body = {"model": self.model, "input": [{"role": "user", "content": content}],
                "metadata": {"include_confidence": "true" if include_confidence else "false",
                             "propagation_direction": propagation}}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            if self._post is not None:
                data = self._post(f"{self.base_url}/responses", headers, body)
            else:
                req = urllib.request.Request(f"{self.base_url}/responses", data=json.dumps(body).encode(),
                                             method="POST", headers=headers)
                with urllib.request.urlopen(req, timeout=120) as response:
                    data = json.loads(response.read().decode())
        except Exception as exc:
            return {"error": f"SAM failed: {type(exc).__name__}: {exc}"}
        text = _extract_output_text(data) if isinstance(data, dict) else None
        if text is None:
            return {"error": "SAM failed: no output_text lane in response"}
        segments = parse_sam_output(text)
        return {"segments": [_segment_dict(s) for s in segments], "raw": text[:4000]}


class FakeSAMClient:
    WIRE = ("<0f>0<|box;x1=10;y1=20;x2=110;y2=120;w=640;h=480;c=0.92|>"
            "<|mask;x=0;y=0;c=0.9;data=20,20,~!!!!M!0c[0o91w|>"
            ",1<|box;x1=200;y1=200;x2=300;y2=260;w=640;h=480|>"
            "<|mask;x=0;y=0;data=12,10,!!!!!(QO(0lu8?|>")

    def __init__(self, *args, **kwargs):
        pass

    def segment(self, images, input_text, propagation="both", include_confidence=True):
        segments = parse_sam_output(self.WIRE)
        return {"segments": [_segment_dict(s) for s in segments], "raw": self.WIRE}


def sam_segment(image_paths: list, prompt: str, api_key: str, base_url: str, model: str = MODEL_DEFAULT, post=None) -> dict:
    """Convenience wrapper: segment images or frames for a noun phrase, for a video edit pipeline."""
    client = SAMClient(base_url, api_key, model, post=post)
    return client.segment(image_paths, build_object_prompt(prompt))
