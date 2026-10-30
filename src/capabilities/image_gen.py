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
code, in src/capabilities/charts.py, with the data in the SVG text,
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


def _image_bytes(data, label: str = "Muse Image") -> bytes:
    """Pull the first image out of an API response, or raise."""
    images = data.get("data", []) if isinstance(data, dict) else []
    if not images:
        raise ValueError(f"{label} did not return an image")
    first = images[0]
    if isinstance(first, dict) and first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if isinstance(first, dict) and first.get("url"):
        req = urllib.request.Request(first["url"])
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    raise ValueError(f"{label} returned an image entry with no bytes and no URL")


def generate_image(prompt: str, api_key: str, endpoint: str,
                   size: str = "1024x1024", post=None, model: str = MODEL) -> bytes:
    """Generate one image, or raise. Approval is the caller's job, before this call."""
    if not prompt.strip():
        raise ValueError("image prompt is required")
    if not api_key:
        raise ValueError("Muse Image API key is required, generation is credit-gated, never run without a key")
    url = endpoint.rstrip("/") + "/images/generations"
    payload = {"model": model, "prompt": prompt, "size": size}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if post is not None:
        data = post(url, headers, payload)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    return _image_bytes(data)


def edit_image(prompt: str, api_key: str, endpoint: str,
               reference_images: list, size: str = "1024x1024", post=None, model: str = MODEL) -> bytes:
    """Edit or compose from reference images, 1 to 10, not text-to-image alone.

    This is the second Muse Image endpoint, POST /v1/images/edits,
    not POST /v1/images/generations. Generations takes a prompt
    only, no reference image. Edits takes a prompt plus 1 to 10
    reference images, and combines them into one scene, or edits
    the first one, as the prompt says.

    How to refer to them: in the prompt, address each reference
    as @Image1, @Image2, and so on, numbered in input order.
    Example: "Combine @Image1, the product, with @Image2, the
    background, keep @Image1's label unchanged, match @Image2's
    lighting." Tagging the role, subject, product, environment,
    or palette, is what makes composition predictable. Untagged
    references tend to average themselves.

    reference_images is a list of image bytes, or image file
    paths, or image URLs, 1 to 10, more than 10 raises, 0 raises.
    Bytes and paths are sent as image files, URLs are sent as
    image URLs, so the caller does not need to download a URL
    first. As with generate_image, approval and credit gating
    are the caller's job, before this call, and a live run
    against the official Meta endpoint is still untested here,
    so the first live edit should be checked against the
    official dev.meta.ai images/edits field names, because
    third-party wrappers differ, some use an images_list,
    some use image_1 to image_10, and the official
    OpenAI-compatible form is image files plus prompt.
    """
    if not prompt.strip():
        raise ValueError("image edit prompt is required")
    if not api_key:
        raise ValueError("Muse Image API key is required, generation is credit-gated, never run without a key")
    if not reference_images or len(reference_images) > 10:
        raise ValueError("Muse Image edit needs 1 to 10 reference images, not 0, not more than 10")
    url = endpoint.rstrip("/") + "/images/edits"
    payload = {"model": model, "prompt": prompt, "size": size,
               "images": list(reference_images)}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if post is not None:
        data = post(url, headers, payload)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    return _image_bytes(data, "Muse Image edit")


def edit_image_files(prompt: str, api_key: str, endpoint: str,
                     reference_images: list, size: str = "1024x1024",
                     post=None, model: str = MODEL) -> bytes:
    """Edit/compose from local image files with multipart/form-data.

    The JSON form above passes paths/URLs through as strings, which the
    OpenAI-compatible ``/images/edits`` endpoint does not accept for local
    files. This variant reads the bytes and posts the official multipart
    form: model, prompt, size, and one ``image[]`` file per reference.

    ``post(url, headers, body_bytes)`` is injectable for tests. Bytes and
    paths are accepted; URLs are not (download them first).
    """
    if not prompt.strip():
        raise ValueError("image prompt is required")
    if not api_key:
        raise ValueError("Muse Image API key is required, generation is credit-gated, never run without a key")
    if not reference_images or len(reference_images) > 10:
        raise ValueError("Muse Image edit needs 1 to 10 reference images, not 0, not more than 10")
    import mimetypes
    import uuid
    files = []
    for reference in reference_images:
        path = Path(reference)
        if not path.is_file():
            raise ValueError(f"reference image not found: {reference}")
        files.append((path, path.read_bytes(),
                      mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
    boundary = "----Aidon" + uuid.uuid4().hex
    body = bytearray()
    for name, value in (("model", model), ("prompt", prompt), ("size", size)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{name}"\r\n\r\n{value}\r\n').encode()
    for path, data, content_type in files:
        filename = path.name.replace('"', "")
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="image[]"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    url = endpoint.rstrip("/") + "/images/edits"
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if post is not None:
        data = post(url, headers, bytes(body))
    else:
        req = urllib.request.Request(url, data=bytes(body), method="POST",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())
    return _image_bytes(data, "Muse Image edit")


def save_image(data: bytes, path: Path) -> Path:
    path = Path(path)
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        path = path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
