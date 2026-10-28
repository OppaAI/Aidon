"""Remote hub, Aidon remote_hub concept, rebuilt as a local hub directory, honestly named.

A hub here is a directory under <home>/hub/, where a plugin or skill can be published as a
copy, listed, and fetched back, so a team can share a plugin without a central server.
This is NOT a remote server, and it does not claim to publish to a public registry: publish
copies files to the hub dir, fetch copies them back, and list shows what is in the hub.
A future remote transport would be a new module, not a silent change to this one.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class LocalHub:
    def __init__(self, home):
        self.dir = Path(home) / "hub"
        self.dir.mkdir(parents=True, exist_ok=True)

    def publish(self, name: str, source_dir) -> Path:
        source = Path(source_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"no plugin dir {source}")
        dest = self.dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        return dest

    def list(self) -> list:
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir())

    def fetch(self, name: str, dest_dir) -> Path:
        source = self.dir / name
        if not source.is_dir():
            raise FileNotFoundError(f"no hub entry {name}")
        dest = Path(dest_dir) / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        return dest
