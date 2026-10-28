"""Uploads, Aidon uploads concept, rebuilt as a separate module.

An upload is a file copied into <home>/uploads/ with a safe filename, listed, read, and
deleted by name, so a WebUI upload, a WhatsApp media file, or a CLI file can be stored
in one place, with the same rules, instead of each transport inventing its own store.
Safe filename means no path separators, no parent directory escape, ever.
"""

from __future__ import annotations

import re
from pathlib import Path


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(name).name)[:80]
    return safe or "upload"


class UploadStore:
    def __init__(self, home):
        self.dir = Path(home) / "uploads"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data: bytes) -> Path:
        path = self.dir / _safe_name(name)
        path.write_bytes(data)
        return path

    def list(self) -> list:
        return sorted(p.name for p in self.dir.iterdir() if p.is_file())

    def read(self, name: str) -> bytes | None:
        path = self.dir / _safe_name(name)
        return path.read_bytes() if path.is_file() else None

    def delete(self, name: str) -> bool:
        path = self.dir / _safe_name(name)
        if not path.is_file():
            return False
        path.unlink()
        return True
