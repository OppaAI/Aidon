"""API keys, old Aidon apikeys concept, rebuilt, hash-only storage, same rule as v1 keys.

A key is generated once, shown once to the caller, and only its SHA256 hash is stored,
in a 0600 file under <home>/apikeys/keys.json, with a name and scope. Verification is
hash comparison, the stored file never contains a usable key, and listing shows hash
prefixes only, never a real key. This is for Aidon's own API keys, not for external service
tokens, which belong in .env, never in this store.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path


class ApiKeyStore:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / "apikeys" / "keys.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")
            self.path.chmod(0o600)

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list) -> None:
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def create_key(self, name: str, scope: str = "agent") -> dict:
        key = "aidon_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        items = self._load()
        items.append({"name": name, "scope": scope, "key_hash": digest, "key_hash_prefix": digest[:8]})
        self._save(items)
        return {"key": key, "name": name, "scope": scope, "key_hash_prefix": digest[:8]}

    def verify(self, key: str) -> dict | None:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for item in self._load():
            if item.get("key_hash") == digest:
                return item
        return None

    def list_keys(self) -> list:
        return [{"name": item.get("name"), "scope": item.get("scope"), "key_hash_prefix": item.get("key_hash_prefix")} for item in self._load()]

    def revoke(self, name: str) -> bool:
        items = self._load()
        remaining = [i for i in items if i.get("name") != name]
        if len(remaining) == len(items):
            return False
        self._save(remaining)
        return True
