"""Library: image inputs, artifacts, and outputs, under the Aidon home.

Files are NOT stored in the repository, and they are NOT sent
to a central service. They are copied into the user's own
private Aidon home, at:

    <home>/library/files/<id>/<name>

where <home> is ``~/.aidon`` by default, or ``AIDON_HOME``.
That is the same sub-path shape as the earlier design, so the
WebUI Library tab can reuse it unchanged when the WebUI is
built: left sidebar, Library view, thumbnails for images,
a ``memory_linked`` flag, click to open, delete to remove.

A library item is a manifest entry, not a database row:
``<home>/library_manifest.json`` holds id, name, kind,
conversation_id, stored path (relative to home),
size, mime, created time, and memory_id when linked. No
second database is introduced; Qdrant remains the only
memory store, and the manifest is just the file index.

Memory link, the key rule:
- An input image, a generated image, a chart, or any output
  is copied into the library first, then recorded in memory
  (chat history / STM, episodic, or LTM on promotion) with
  its link in the payload: library_id, library_path,
  library_url (``/api/library/<id>/file``, the future WebUI
  route), and kind.
- Recalling the text therefore returns the link, so the
  caller can show or open the image alongside the text.
- Promotion episodic -> LTM copies the payload, so the
  library link survives promotion; it does not have to be
  re-linked.
- Charts carry their data file as well: the chart image is
  the visual, the data file (copied into the same item
  directory) is the numbers, because a vector cannot
  recover exact values from a picture.

Serving confinement, same as the earlier design: a library
file is only served if its resolved path is under
``<home>/library``. Never serve an arbitrary path.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import time
import uuid
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def is_image(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def library_url(item_id: str) -> str:
    return f"/api/library/{item_id}/file"


class Library:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.root = self.home / "library" / "files"
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.home / "library_manifest.json"

    def _manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=2))

    def add_file(self, source: Path, kind: str = "attachment",
                 conversation_id: str = "",
                 description: str = "") -> dict:
        """Copy a file into the library and index it."""
        source = Path(source)
        item_id = uuid.uuid4().hex[:12]
        dest_dir = self.root / item_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / source.name
        shutil.copyfile(source, target)
        item = {
            "id": item_id,
            "name": source.name,
            "kind": "image" if is_image(source) else kind,
            "conversation_id": conversation_id,
            "description": description,
            "stored_path": str(target.relative_to(self.home)),
            "size": target.stat().st_size,
            "mime": mimetypes.guess_type(source.name)[0] or "",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "memory_id": "",
            "url": library_url(item_id),
            "memory_linked": False,
        }
        manifest = self._manifest()
        manifest[item_id] = item
        self._save_manifest(manifest)
        return item

    def add_image(self, source: Path, description: str,
                  memory=None, collection: str = "episodic",
                  conversation_id: str = "",
                  data_path: Path | None = None) -> tuple[dict, object | None]:
        """Library copy + memory point, with the link in the payload.

        Returns (item, point). Point is None when no memory is
        given; the file is still in the library, and can be
        linked later with link_memory().
        If data_path is given (a chart's data file), it is
        copied into the same item directory, and its name is
        recorded as data_file, so the chart and its numbers
        travel together.
        """
        item = self.add_file(source, kind="image",
                             conversation_id=conversation_id,
                             description=description)
        if data_path is not None:
            data_target = self.root / item["id"] / Path(data_path).name
            shutil.copyfile(data_path, data_target)
            item["data_file"] = Path(data_path).name
            manifest = self._manifest()
            manifest[item["id"]] = item
            self._save_manifest(manifest)
        point = None
        if memory is not None:
            payload = {
                "library_id": item["id"],
                "library_path": item["stored_path"],
                "library_url": item["url"],
                "conversation_id": conversation_id,
                "file": item["name"],
            }
            if data_path is not None:
                payload["data_file"] = item["data_file"]
            if collection == "episodic":
                point = memory.record_episodic(
                    conversation_id, description, payload=payload,
                    attachments=[item["id"]])
            else:
                point = memory.record_image(collection,
                                            self.home / item["stored_path"],
                                            description, payload=payload)
            linked = self.link_memory(item["id"], point.id)
            if linked is not None:
                item = linked
        return item, point

    def link_memory(self, item_id: str, memory_id: str) -> dict | None:
        manifest = self._manifest()
        item = manifest.get(item_id)
        if item is None:
            return None
        item["memory_id"] = memory_id
        item["memory_linked"] = True
        self._save_manifest(manifest)
        return item

    def list_items(self) -> list[dict]:
        """Every library item, newest last; the library is global."""
        items = list(self._manifest().values())
        return sorted(items, key=lambda i: i.get("created_at", ""))

    def get_item(self, item_id: str) -> dict | None:
        item = self._manifest().get(item_id)
        if item is None:
            return None
        # Confinement: resolved path must stay under home/library.
        target = (self.home / item["stored_path"]).resolve()
        try:
            target.relative_to((self.home / "library").resolve())
        except (ValueError, OSError):
            return None
        if not target.is_file():
            return None
        return {**item, "absolute_path": str(target)}

    def delete_item(self, item_id: str) -> bool:
        manifest = self._manifest()
        item = manifest.pop(item_id, None)
        if item is None:
            return False
        dest_dir = self.root / item_id
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        self._save_manifest(manifest)
        return True
