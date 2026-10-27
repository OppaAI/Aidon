"""WhatsApp media to vision, preparation only.

This helper prepares a WhatsApp media attachment for a vision
model call. It does not send anything, and it does not call
any model. The parent wires it into a run: download the media
to the library, prepare a vision message with this helper,
then pass the result to a run that can accept an image.

Rules:
- media_path may be a local file path, a URL, or a media id.
  If a downloader is provided and the path is not already a
  local file that exists, the downloader is called with the
  path and must return a local path (str or Path).
- After download, the file must exist, otherwise ValueError.
  A missing file is never replaced with a placeholder image.
- The image message uses a data URL only for small files
  (at most 1 MiB). Larger files use a file reference instead,
  so a large attachment is not copied into a prompt as a
  huge base64 string.
- library_path is the local path, for the parent to copy
  into the library if it wants to keep the media. This
  helper itself does not move or copy any file.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

_SMALL_LIMIT_BYTES = 1024 * 1024


def _mime_for(path: Path) -> str:
    mime, _encoding = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")


def prepare_vision_message(media_path, goal: str, downloader=None) -> dict:
    """Prepare a vision message for a WhatsApp media attachment.

    Returns a dict with keys goal, image_path, image_message,
    and library_path. Raises ValueError if the file does not
    exist after download, or if the goal is empty.
    """
    if not goal or not str(goal).strip():
        raise ValueError("goal is required")
    raw = str(media_path)
    local = Path(raw)
    if downloader is not None and not local.is_file():
        downloaded = downloader(raw)
        if downloaded is None:
            raise ValueError(f"downloader did not return a path for {raw}")
        local = Path(str(downloaded))
    if not local.is_file():
        raise ValueError(f"media file does not exist: {local}")
    data = local.read_bytes()
    if len(data) <= _SMALL_LIMIT_BYTES:
        mime = _mime_for(local)
        encoded = base64.b64encode(data).decode("ascii")
        image_message = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }
    else:
        image_message = {
            "type": "image_url",
            "image_url": {"url": str(local)},
            "path": str(local),
        }
    return {
        "goal": goal,
        "image_path": str(local),
        "image_message": image_message,
        "library_path": str(local),
    }
