"""Muse Image generation, for 2D assets and images, approval and credit aware.

Muse Image 1.0 is a Meta Model API image model, POST
/v1/images/generations, with a prompt, and optional size.
It is optional, only if a workflow requires an image, never
by default, and never a core requirement, as decided. It
consumes API credits, so generation is an approval-gated
action in the loop, not a free tool call, and the caller
must pass a gate or an explicit approval, otherwise the
call is refused, not run.

What it is for: 2D assets, illustrations, and generated
images, where the image itself is the deliverable, for
example a game sprite, a cover illustration, or a marketing
image. It is not for charts or graphs, because a chart's
numbers must be exact, and an image model cannot be trusted
to draw exact numbers, labels, or bars. Charts are made by
code, in src/charts.py, with the data in the SVG text,
verifiable by code, not by looking at pixels.

How it works, stdlib only:
- generate_image(prompt, api_key, endpoint, size, post)
  posts to <endpoint>/images/generations, with model
  muse-image-1.0, prompt, and size, and returns image
  bytes, from a base64 field or a URL field, never invented.
  post is injectable for tests, taking (url, headers,
  payload) and returning a dict, so no network is used
  in tests, and no real key is used in tests.
- save_image(bytes, path) writes the bytes to a file
  inside the Aidon home, with a .png suffix, and returns
  the path, so the image can be put in the library.
- If the API returns no image, or an error, the function
  raises, it does not return a blank image or a placeholder.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path

MODEL = "muse-image-1.0"


def generate_image(prompt: str, api_key: str, endpoint: str,
                   size: str = "1024x1024", post=None) -> bytes:
    """Generate one image, or raise. Approval is the caller's job, before this call."""
    if not prompt.strip():
        raise ValueError("image prompt is required")
    if not api_key:
        raise ValueError("Muse Image API key is required, generation is credit-gated, never run without a key")
    url = endpoint.rstrip("/") + "/images/generations"
    payload = {"model": MODEL, "prompt": prompt, "size": size}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if post is not None:
        data = post(url, headers, payload)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    images = data.get("data", []) if isinstance(data, dict) else []
    if not images:
        raise ValueError("Muse Image did not return an image")
    first = images[0]
    if isinstance(first, dict) and first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if isinstance(first, dict) and first.get("url"):
        req = urllib.request.Request(first["url"])
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    raise ValueError("Muse Image returned an image entry with no bytes and no URL")


def save_image(data: bytes, path: Path) -> Path:
    path = Path(path)
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        path = path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
