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
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Read an int, warn and use the default on a typo.

    A bad value in .env used to raise ValueError inside Config.load,
    which meant every command, including `tools list`, died with a
    traceback because of one stray character in an unrelated key.
    """
    raw = os.environ.get(name, "")
    if not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        print(f"warning: {name}={raw!r} is not an integer, "
              f"using {default}", file=sys.stderr)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        print(f"warning: {name}={raw!r} is not a number, "
              f"using {default}", file=sys.stderr)
        return default


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
    sandbox_mem_mb: int = 1024
    sandbox_nproc: int = 256
    sandbox_fsize_mb: int = 512
    sandbox_cpu_seconds: int = 0
    sandbox_backend: str = "auto"
    sandbox_network: bool = False
    sandbox_require_isolation: bool = False
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
    # Messaging, old key names, see src/interface/messaging.py.
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_owner: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    # Old capability keys, only, no new duplicates.
    search_backend: str = "tavily"
    searxng_url: str = "http://localhost:8888"
    tavily_api_key: str = ""
    youtube_api_key: str = ""
    fish_api_key: str = ""
    tts_voice_id: str = ""
    transcribe_api_key: str = ""
    threads_access_token: str = ""
    threads_account_id: str = ""
    ig_user_id: str = ""
    ig_access_token: str = ""
    github_token: str = ""
    daily_report_time: str = ""
    approval_nudge_minutes: int = 15
    approval_nudge_channel: str = ""
    autonomy_enabled: bool = True
    autonomy_threshold: int = 5
    autonomy_decay_days: float = 14.0
    owner_email: str = ""
    corrected_subject: bool = True
    # Old routing / kernel keys, only, no new duplicates.
    fast_model: str = ""
    verifier_model: str = ""
    voice_announce: bool = False
    sam_base_url: str = "https://api.meta.ai/v1"
    sam_api_key: str = ""
    learn_max_skills_per_day: int = 5
    skill_max_failures: int = 3
    # Backend selection, old Aidon keys: AIDON_BACKEND decides
    # between a local Ollama, the Meta Model API, a plain
    # OpenAI-compatible endpoint, and the offline fake client.
    backend: str = ""
    ollama_endpoint: str = "http://localhost:11434"
    ollama_think: bool = False
    ollama_num_ctx: int = 0
    fast_num_ctx: int = 0
    # Research caps, old Aidon names only, no new duplicates.
    # AIDON_RESEARCH_ALLOWLIST is a comma list, empty = unrestricted.
    research_max_rounds: int = 10
    max_stuck_iterations: int = 8
    parallel_tools: int = 4
    research_allowlist: str = ""
    webui_token: str = ""
    image_model: str = "muse-image-1.0"
    sam_model: str = "meta-sam"
    router_fast_phases: str = "interpret,learn,respond"
    kernel_mode: bool = False

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        load_dotenv()
        home = home or default_home()
        return cls(
            home=Path(home),
            model=os.environ.get("AIDON_MODEL", "muse-spark"),
            # Old key names only. A duplicate new key for the
            # same setting was removed, not kept as an alias,
            # so there is exactly one name per setting.
            api_base=os.environ.get("AIDON_API_ENDPOINT", ""),
            api_key=os.environ.get("AIDON_API_KEY", ""),
            embed_model=os.environ.get("AIDON_EMBED_MODEL",
                                       "google/embeddinggemma-2"),
            sandbox_timeout_seconds=_env_int(
                "AIDON_MAX_SANDBOX_SECONDS", 120),
            sandbox_mem_mb=_env_int("AIDON_SANDBOX_MEM_MB", 1024),
            sandbox_nproc=_env_int("AIDON_SANDBOX_NPROC", 256),
            sandbox_fsize_mb=_env_int("AIDON_SANDBOX_FSIZE_MB", 512),
            sandbox_cpu_seconds=_env_int("AIDON_SANDBOX_CPU_S", 0),
            sandbox_backend=os.environ.get("AIDON_SANDBOX_BACKEND", "auto"),
            sandbox_network=os.environ.get("AIDON_SANDBOX_NETWORK", "0") == "1",
            sandbox_require_isolation=os.environ.get(
                "AIDON_SANDBOX_REQUIRE_ISOLATION", "0") == "1",
            max_steps=_env_int("AIDON_MAX_STEPS", 50),
            approval_timeout_seconds=_env_int(
                "AIDON_APPROVAL_TIMEOUT_S", 180),
            execution_backend=os.environ.get("AIDON_EXECUTION_BACKEND",
                                             "bubblewrap"),
            docker_image=os.environ.get("AIDON_DOCKER_IMAGE",
                                        "python:3.12-slim"),
            docker_network=os.environ.get("AIDON_DOCKER_NETWORK",
                                          "0") == "1",
            ingest_dir=(Path(os.environ["AIDON_INGEST_DIR"]).expanduser()
                        if os.environ.get("AIDON_INGEST_DIR") else None),
            budget_tokens=_env_int(
                "AIDON_MAX_TOKENS_PER_RUN", 200000),
            budget_seconds=int(_env_float(
                "AIDON_MAX_WALLCLOCK_MIN", 60) * 60),
            model_timeout_seconds=_env_float(
                "AIDON_MODEL_TIMEOUT_S", 120),
            timezone=os.environ.get("AIDON_TIMEZONE", "UTC"),
            forget_half_life_days=_env_float(
                "AIDON_FORGET_HALF_LIFE_DAYS", 21),
            forget_cleanup_threshold=_env_float(
                "AIDON_FORGET_CLEANUP_THRESHOLD", 0.02),
            forget_grace_days=_env_int(
                "AIDON_FORGET_GRACE_DAYS", 35),
            whatsapp_token=os.environ.get("AIDON_WHATSAPP_TOKEN", ""),
            whatsapp_phone_id=os.environ.get("AIDON_WHATSAPP_PHONE_ID", ""),
            whatsapp_verify_token=os.environ.get(
                "AIDON_WHATSAPP_VERIFY_TOKEN", ""),
            whatsapp_app_secret=os.environ.get("AIDON_WHATSAPP_APP_SECRET", ""),
            whatsapp_owner=os.environ.get("AIDON_WHATSAPP_OWNER", ""),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_signing_secret=os.environ.get(
                "SLACK_SIGNING_SECRET",
                os.environ.get("AIDON_SLACK_SIGNING_SECRET", "")),
            search_backend=os.environ.get("AIDON_SEARCH_BACKEND", "tavily"),
            searxng_url=os.environ.get("AIDON_SEARXNG_URL", "http://localhost:8888"),
            tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
            youtube_api_key=os.environ.get("AIDON_YOUTUBE_API_KEY", ""),
            fish_api_key=os.environ.get("AIDON_FISH_API_KEY", ""),
            tts_voice_id=os.environ.get("AIDON_TTS_VOICE_ID", ""),
            transcribe_api_key=os.environ.get("AIDON_TRANSCRIBE_API_KEY", ""),
            threads_access_token=os.environ.get("AIDON_THREADS_ACCESS_TOKEN", ""),
            threads_account_id=os.environ.get("AIDON_THREADS_ACCOUNT_ID", ""),
            ig_user_id=os.environ.get("AIDON_IG_USER_ID", ""),
            ig_access_token=os.environ.get("AIDON_IG_ACCESS_TOKEN", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            daily_report_time=os.environ.get("AIDON_DAILY_REPORT_TIME", ""),
            approval_nudge_minutes=_env_int("AIDON_APPROVAL_NUDGE_MIN", 15),
            approval_nudge_channel=os.environ.get("AIDON_APPROVAL_NUDGE_CHANNEL", ""),
            autonomy_enabled=os.environ.get("AIDON_AUTONOMY_ENABLED", "1") not in ("0", "false", "no"),
            autonomy_threshold=_env_int("AIDON_AUTONOMY_THRESHOLD", 5),
            autonomy_decay_days=_env_float("AIDON_AUTONOMY_DECAY_DAYS", 14),
            owner_email=os.environ.get("AIDON_OWNER_EMAIL", ""),
            corrected_subject=os.environ.get("AIDON_CORRECTED_SUBJECT", "1") not in ("0", "false", "no"),
            fast_model=os.environ.get("AIDON_FAST_MODEL", ""),
            verifier_model=os.environ.get("AIDON_VERIFIER_MODEL", ""),
            voice_announce=os.environ.get("AIDON_VOICE_ANNOUNCE", "0") not in ("0", "false", "no", ""),
            sam_base_url=os.environ.get("AIDON_SAM_BASE_URL", "https://api.meta.ai/v1"),
            sam_api_key=os.environ.get("AIDON_SAM_API_KEY", ""),
            learn_max_skills_per_day=_env_int(
                "AIDON_LEARN_MAX_SKILLS_PER_DAY", 5),
            skill_max_failures=_env_int("AIDON_SKILL_MAX_FAILURES", 3),
            backend=os.environ.get("AIDON_BACKEND", "").strip().lower(),
            ollama_endpoint=os.environ.get(
                "AIDON_OLLAMA_ENDPOINT", "http://localhost:11434"),
            ollama_think=os.environ.get("AIDON_OLLAMA_THINK", "0") not in
            ("0", "false", "no", ""),
            ollama_num_ctx=_env_int("AIDON_OLLAMA_NUM_CTX", 0),
            fast_num_ctx=_env_int("AIDON_FAST_NUM_CTX", 0),
            research_max_rounds=_env_int("AIDON_RESEARCH_MAX_ROUNDS", 10),
            max_stuck_iterations=_env_int("AIDON_MAX_STUCK_ITERATIONS", 8),
            parallel_tools=_env_int("AIDON_PARALLEL_TOOLS", 4),
            research_allowlist=os.environ.get("AIDON_RESEARCH_ALLOWLIST", ""),
            webui_token=os.environ.get("AIDON_WEBUI_TOKEN", ""),
            image_model=os.environ.get("AIDON_IMAGE_MODEL", "muse-image-1.0"),
            sam_model=os.environ.get("AIDON_SAM_MODEL", "meta-sam"),
            router_fast_phases=os.environ.get("AIDON_ROUTER_FAST_PHASES", "interpret,learn,respond"),
            kernel_mode=os.environ.get("AIDON_KERNEL", "0") not in ("0", "false", "no"),
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
