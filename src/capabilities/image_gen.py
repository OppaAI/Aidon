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
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "muse-image-1.0"


def image_api_config(config) -> tuple[str, str]:
    """Resolve image credentials without sending a chat provider's key elsewhere."""
    from urllib.parse import urlsplit
    main = str(getattr(config, "api_base", "") or "").strip().rstrip("/")
    endpoint = str(getattr(config, "image_api_endpoint", "") or "").strip().rstrip("/")
    key = str(getattr(config, "image_api_key", "") or "")
    if not endpoint:
        endpoint = (main if urlsplit(main).hostname == "api.meta.ai"
                    else "https://api.meta.ai/v1")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("AIDON_IMAGE_API_ENDPOINT must be an API base URL such as https://api.meta.ai/v1")
    if not key and main:
        origin = lambda url: (url.scheme, url.hostname, url.port)
        if origin(parsed) == origin(urlsplit(main)):
            key = str(getattr(config, "api_key", "") or "")
    if not key:
        raise ValueError("Set AIDON_IMAGE_API_KEY (Meta Model API key) in .env or Settings. "
                         "The chat provider key cannot be used for Muse Image at another provider. "
                         "AIDON_IMAGE_API_ENDPOINT defaults to https://api.meta.ai/v1.")
    return endpoint, key


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
        data = _post_image_json(url, headers, payload)
    return _image_bytes(data)


def _reference_input(reference) -> dict:
    if isinstance(reference, dict):
        if set(reference) not in ({"image_url"}, {"file_id"}):
            raise ValueError("reference needs image_url or file_id")
        return dict(reference)
    if isinstance(reference, str) and reference.startswith(("https://", "http://", "data:image/")):
        return {"image_url": reference}
    if isinstance(reference, bytes):
        data, mime = reference, "image/png"
    else:
        path = Path(reference)
        if not path.is_file():
            raise ValueError(f"reference image not found: {reference}")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not data:
        raise ValueError("reference image is empty")
    return {"image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}


def _post_image_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read(4096).decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                error = parsed.get("error", {})
                detail = error.get("message", "") if isinstance(error, dict) else error
                detail = detail or parsed.get("detail") or parsed.get("message") or ""
            detail = str(detail)[:500]
            # Provider messages occasionally echo authentication inputs.
            key = headers.get("Authorization", "").removeprefix("Bearer ")
            if key:
                detail = detail.replace(key, "[redacted]")
        except Exception:
            pass
        from urllib.parse import urlsplit
        parsed_url = urlsplit(url)
        destination = (parsed_url.hostname or "") + parsed_url.path
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Muse Image HTTP {exc.code} at {destination}{suffix}") from None


def edit_image(prompt: str, api_key: str, endpoint: str,
               reference_images: list, size: str = "1024x1024", post=None,
               model: str = MODEL, output_format: str = "png") -> bytes:
    """Edit 1–10 inputs using Meta's images/image_url JSON contract.

    Inputs can be local paths, bytes, public/data URLs, or image_url/file_id
    objects. Local files are encoded here, never sent as server-side paths.
    post(url, headers, payload_dict) can replace the HTTP transport in tests.
    """
    if not prompt.strip():
        raise ValueError("image edit prompt is required")
    if not api_key:
        raise ValueError("Muse Image API key is required")
    if not reference_images or len(reference_images) > 10:
        raise ValueError("Muse Image edit needs 1 to 10 reference images")
    if output_format not in ("png", "jpeg", "webp"):
        raise ValueError("image output format must be png, jpeg, or webp")
    url = endpoint.rstrip("/") + "/images/edits"
    payload = {"model": model, "prompt": prompt, "size": size,
               "images": [_reference_input(ref) for ref in reference_images],
               "n": 1, "response_format": "b64_json", "output_format": output_format}
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json", "Accept": "application/json"}
    data = (post or _post_image_json)(url, headers, payload)
    return _image_bytes(data, "Muse Image edit")


def edit_image_files(prompt: str, api_key: str, endpoint: str,
                     reference_images: list, size: str = "1024x1024",
                     post=None, model: str = MODEL, output_format: str = "png") -> bytes:
    """Local-file entry point; injectable transport receives JSON body bytes."""
    paths = [Path(ref) for ref in reference_images]
    adapter = ((lambda url, headers, payload:
                post(url, headers, json.dumps(payload).encode("utf-8")))
               if post is not None else None)
    return edit_image(prompt, api_key, endpoint, paths, size, adapter,
                      model, output_format)


def save_image(data: bytes, path: Path) -> Path:
    path = Path(path)
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        path = path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
