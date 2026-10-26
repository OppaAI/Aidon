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


def _env_first(*names: str, default: str = "") -> str:
    """First non-empty env var, in order. Old names first, then new."""
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return default


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
    execution_backend: str = "bubblewrap"
    docker_image: str = "python:3.12-slim"
    docker_network: bool = False
    ingest_dir: Path | None = None
    budget_tokens: int = 200000
    budget_seconds: int = 3600
    # Old Aidon knobs, carried over because they have a live
    # consumer in the new code, not as dead settings.
    model_timeout_seconds: float = 120.0
    timezone: str = "UTC"
    forget_half_life_days: float = 21.0
    forget_cleanup_threshold: float = 0.02
    forget_grace_days: int = 35
    # Messaging, old key names, see src/messaging.py.
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_owner: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        load_dotenv()
        home = home or default_home()
        return cls(
            home=Path(home),
            model=os.environ.get("AIDON_MODEL", "muse-spark"),
            # AIDON_API_ENDPOINT is the old name for the same
            # endpoint, AIDON_API_BASE is the new name.
            api_base=_env_first("AIDON_API_BASE", "AIDON_API_ENDPOINT"),
            api_key=os.environ.get("AIDON_API_KEY", ""),
            embed_model=os.environ.get("AIDON_EMBED_MODEL",
                                       "google/embeddinggemma-2"),
            sandbox_timeout_seconds=int(_env_first(
                "AIDON_SANDBOX_TIMEOUT", "AIDON_MAX_SANDBOX_SECONDS",
                default="120")),
            max_steps=int(os.environ.get("AIDON_MAX_STEPS", "50")),
            approval_timeout_seconds=int(_env_first(
                "AIDON_APPROVAL_TIMEOUT", "AIDON_APPROVAL_TIMEOUT_S",
                default="180")),
            execution_backend=os.environ.get("AIDON_EXECUTION_BACKEND",
                                             "bubblewrap"),
            docker_image=os.environ.get("AIDON_DOCKER_IMAGE",
                                        "python:3.12-slim"),
            docker_network=os.environ.get("AIDON_DOCKER_NETWORK",
                                          "0") == "1",
            ingest_dir=(Path(os.environ["AIDON_INGEST_DIR"]).expanduser()
                        if os.environ.get("AIDON_INGEST_DIR") else None),
            budget_tokens=int(_env_first(
                "AIDON_BUDGET_TOKENS", "AIDON_MAX_TOKENS_PER_RUN",
                default="200000")),
            budget_seconds=(int(os.environ["AIDON_BUDGET_SECONDS"])
                              if os.environ.get("AIDON_BUDGET_SECONDS")
                              else int(float(os.environ.get(
                                  "AIDON_MAX_WALLCLOCK_MIN", "60") or 60)) * 60),
            model_timeout_seconds=float(os.environ.get(
                "AIDON_MODEL_TIMEOUT_S", "120") or 120),
            timezone=os.environ.get("AIDON_TIMEZONE", "UTC"),
            forget_half_life_days=float(os.environ.get(
                "AIDON_FORGET_HALF_LIFE_DAYS", "21") or 21),
            forget_cleanup_threshold=float(os.environ.get(
                "AIDON_FORGET_CLEANUP_THRESHOLD", "0.02") or 0.02),
            forget_grace_days=int(os.environ.get(
                "AIDON_FORGET_GRACE_DAYS", "35") or 35),
            whatsapp_token=os.environ.get("AIDON_WHATSAPP_TOKEN", ""),
            whatsapp_phone_id=os.environ.get("AIDON_WHATSAPP_PHONE_ID", ""),
            whatsapp_verify_token=_env_first(
                "AIDON_WHATSAPP_VERIFY_TOKEN", "WHATSAPP_VERIFY_TOKEN"),
            whatsapp_app_secret=os.environ.get("AIDON_WHATSAPP_APP_SECRET", ""),
            whatsapp_owner=os.environ.get("AIDON_WHATSAPP_OWNER", ""),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET", ""),
        )

    @property
    def workdir(self) -> Path:
        return self.home / "workspace"

    @property
    def library_dir(self) -> Path:
        return self.home / "library" / "files"

    @property
    def ingest_path(self) -> Path:
        return self.ingest_dir or (self.home / "ingest")

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        return self.home
