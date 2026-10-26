"""Configuration for Aidon, loaded from the environment and .env.

No secrets are stored in this repository. A user copies
.env.example to .env on their own machine and fills in their own
keys there. That file is gitignored and is never committed.

The home directory is where Aidon keeps its own state on the
user's machine: workdirs, memory, keys, and learned records.
It is outside the repository by design, so a git clone never
carries another user's data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    env_home = os.environ.get("AIDON_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".aidon"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE from a .env file, without overriding real env.

    Looks in the current directory, then in the Aidon home.
    No secrets are read into code beyond os.environ, and a
    missing .env is not an error. Quotes, comments, and an
    ``export `` prefix are handled, nothing else is parsed.
    """
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(default_home() / ".env")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
        return


@dataclass
class Config:
    home: Path
    model: str = "muse-spark"
    api_base: str = ""
    api_key: str = ""
    embed_model: str = "google/embeddinggemma-2"
    sandbox_timeout_seconds: int = 120
    max_steps: int = 50
    approval_timeout_seconds: int = 180

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        load_dotenv()
        home = home or default_home()
        return cls(
            home=Path(home),
            model=os.environ.get("AIDON_MODEL", "muse-spark"),
            api_base=os.environ.get("AIDON_API_BASE", ""),
            api_key=os.environ.get("AIDON_API_KEY", ""),
            embed_model=os.environ.get("AIDON_EMBED_MODEL",
                                       "google/embeddinggemma-2"),
            sandbox_timeout_seconds=int(
                os.environ.get("AIDON_SANDBOX_TIMEOUT", "120")),
            max_steps=int(os.environ.get("AIDON_MAX_STEPS", "50")),
            approval_timeout_seconds=int(
                os.environ.get("AIDON_APPROVAL_TIMEOUT", "180")),
        )

    @property
    def workdir(self) -> Path:
        return self.home / "workspace"

    @property
    def library_dir(self) -> Path:
        return self.home / "library" / "files"

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        return self.home
