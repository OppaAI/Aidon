"""Settings catalog and write validation for the WebUI."""

import json
import math


class WebUISettingsMixin:
    def settings_get(self) -> dict:
        path = self.home / "settings.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def settings_set(self, key: str, value) -> bool:
        allowed = {"timezone", "budget_tokens", "max_steps", "autonomy_enabled", "approval_nudge_minutes",
                   "research_max_rounds", "max_stuck_iterations", "parallel_tools"}
        if key not in allowed:
            return False
        data = self.settings_get()
        data[key] = value
        path = self.home / "settings.json"
        path.write_text(json.dumps(data, indent=2))
        return True

    # -- WebUI depth: settings into .env, studio edits, plugin adoption.
    #    These are new methods only; the existing /api/settings
    #    behaviour above (settings.json, 8 keys) is unchanged. ------
    _ENV_SETTING_MAP = {
        "timezone": "AIDON_TIMEZONE",
        "budget_tokens": "AIDON_MAX_TOKENS_PER_RUN",
        "max_steps": "AIDON_MAX_STEPS",
        "autonomy_enabled": "AIDON_AUTONOMY_ENABLED",
        "approval_nudge_minutes": "AIDON_APPROVAL_NUDGE_MIN",
        "research_max_rounds": "AIDON_RESEARCH_MAX_ROUNDS",
        "max_stuck_iterations": "AIDON_MAX_STUCK_ITERATIONS",
        "parallel_tools": "AIDON_PARALLEL_TOOLS",
        "daily_report_time": "AIDON_DAILY_REPORT_TIME",
        "execution_backend": "AIDON_EXECUTION_BACKEND",
        "search_backend": "AIDON_SEARCH_BACKEND",
        "searxng_url": "AIDON_SEARXNG_URL",
        "qdrant_url": "AIDON_QDRANT_URL",
        "image_model": "AIDON_IMAGE_MODEL",
        "sam_model": "AIDON_SAM_MODEL",
        "owner_email": "AIDON_OWNER_EMAIL",
        "model": "AIDON_MODEL",
        "fast_model": "AIDON_FAST_MODEL",
        "verifier_model": "AIDON_VERIFIER_MODEL",
        "voice_announce": "AIDON_VOICE_ANNOUNCE",
        "sam_base_url": "AIDON_SAM_BASE_URL",
        "sam_api_key": "AIDON_SAM_API_KEY",
        "tts_voice_id": "AIDON_TTS_VOICE_ID",
        "backend": "AIDON_BACKEND",
        "api_endpoint": "AIDON_API_ENDPOINT",
        "embed_model": "AIDON_EMBED_MODEL",
        "sparse_model": "AIDON_SPARSE_MODEL",
        "sandbox_timeout_seconds": "AIDON_MAX_SANDBOX_SECONDS",
        "approval_timeout_seconds": "AIDON_APPROVAL_TIMEOUT_S",
        "docker_image": "AIDON_DOCKER_IMAGE",
        "docker_network": "AIDON_DOCKER_NETWORK",
        "ingest_dir": "AIDON_INGEST_DIR",
        "wallclock_minutes": "AIDON_MAX_WALLCLOCK_MIN",
        "model_timeout_seconds": "AIDON_MODEL_TIMEOUT_S",
        "forget_half_life_days": "AIDON_FORGET_HALF_LIFE_DAYS",
        "forget_grace_days": "AIDON_FORGET_GRACE_DAYS",
        "forget_cleanup_threshold": "AIDON_FORGET_CLEANUP_THRESHOLD",
        "whatsapp_phone_id": "AIDON_WHATSAPP_PHONE_ID",
        "whatsapp_owner": "AIDON_WHATSAPP_OWNER",
        "threads_account_id": "AIDON_THREADS_ACCOUNT_ID",
        "ig_user_id": "AIDON_IG_USER_ID",
        "approval_nudge_channel": "AIDON_APPROVAL_NUDGE_CHANNEL",
        "autonomy_threshold": "AIDON_AUTONOMY_THRESHOLD",
        "autonomy_decay_days": "AIDON_AUTONOMY_DECAY_DAYS",
        "corrected_subject": "AIDON_CORRECTED_SUBJECT",
        "model_think": "AIDON_MODEL_THINK",
        "model_num_ctx": "AIDON_MODEL_NUM_CTX",
        "fast_num_ctx": "AIDON_FAST_NUM_CTX",
        "research_allowlist": "AIDON_RESEARCH_ALLOWLIST",
        "router_fast_phases": "AIDON_ROUTER_FAST_PHASES",
        "kernel": "AIDON_KERNEL",
        "optional_tools": "AIDON_OPTIONAL_TOOLS",
        "tool_posture": "AIDON_TOOL_POSTURE",
        "computer_driver": "AIDON_COMPUTER_DRIVER",
        # Historical spelling remains writable for old installations.
        # New configuration uses the canonical Slack variable below.
        "AIDON_SLACK_SIGNING_SECRET": "AIDON_SLACK_SIGNING_SECRET",
    }
    _ENV_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CLIENT")

    # Settings catalog: every live Config key, grouped, with type,
    # default, and secret flag. Legacy Meta keys and AIDON_DEPTH are
    # absent. Verifier model and think phases remain live because they
    # are needed when using local model backends. Float-valued keys use type "string" (the
    # catalog has only string/int/bool/enum) and are validated
    # as numbers on write. Secrets are listed so the catalog
    # read can mask them and report is_set, never the value.
    SETTINGS_CATALOG = [
        {"group": "Interface", "settings": [
            {"key": "AIDON_WEBUI_TOKEN", "label": "WebUI token", "type": "string", "default": "", "desc": "Bearer token for the WebUI API.", "secret": True},
            {"key": "AIDON_WEBUI_PORT", "label": "WebUI port", "type": "int", "default": "8765", "desc": "Port used by launch.sh when starting the WebUI.", "secret": False},
            {"key": "AIDON_WEBUI_ALLOWED_HOSTS", "label": "WebUI allowed hosts", "type": "string", "default": "", "desc": "Additional Host names accepted by the WebUI.", "secret": False},
            {"key": "AIDON_VERSION", "label": "Displayed version override", "type": "string", "default": "", "desc": "Displayed release version override; empty = installed package version.", "secret": False},
            {"key": "AIDON_VRM_PATH", "label": "Avatar VRM path", "type": "string", "default": "", "desc": "Optional absolute path to the avatar VRM model.", "secret": False},
        ]},
        {"group": "Scheduler", "settings": [
            {"key": "AIDON_DAILY_REPORT_TIME", "label": "Daily report time", "type": "string", "default": "", "desc": "Time of day for the daily report.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_MIN", "label": "Approval nudge minutes", "type": "int", "default": "15", "desc": "Minutes before an approval card is nudged.", "secret": False},
        ]},
        {"group": "System", "settings": [
            {"key": "AIDON_APPROVAL_TIMEOUT_S", "label": "Approval timeout seconds", "type": "int", "default": "900", "desc": "Seconds an approval card waits before failing closed.", "secret": False},
            {"key": "AIDON_EXECUTION_BACKEND", "label": "Execution backend", "type": "enum", "default": "bubblewrap", "desc": "Sandbox backend for shell steps: bubblewrap (local isolation), docker (disposable container), subprocess (no isolation), vm (SSH to a VM you run; needs AIDON_VM_HOST), or modal (Modal cloud sandboxes; needs AIDON_MODAL_APP and `modal setup` on first use). The planner never chooses this; only the owner sets it. Default: bubblewrap.", "secret": False, "options": ["bubblewrap", "docker", "subprocess", "vm", "modal"]},
            {"key": "AIDON_DOCKER_IMAGE", "label": "Docker image", "type": "string", "default": "python:3.12-slim", "desc": "Pinned image for the Docker backend.", "secret": False},
            {"key": "AIDON_DOCKER_NETWORK", "label": "Docker network", "type": "bool", "default": "0", "desc": "Allow network for the Docker backend.", "secret": False},
            {"key": "AIDON_VM_HOST", "label": "VM host", "type": "string", "default": "", "desc": "Hostname or IP of the VM the VM backend SSHes into. Only used when the execution backend is vm; without it the VM backend refuses to run. Default: empty.", "secret": False},
            {"key": "AIDON_VM_USER", "label": "VM user", "type": "string", "default": "", "desc": "SSH user for the VM backend. Only used when the execution backend is vm; empty means your current login. Default: empty.", "secret": False},
            {"key": "AIDON_VM_USE_SUDO", "label": "VM sudo", "type": "bool", "default": "0", "desc": "Run VM commands under sudo -S inside the VM, so runs that need system access work there instead of on the host. Needs AIDON_VM_SUDO_PASSWORD; without it, sudo runs fail closed instead of prompting. Only affects the VM backend. Default: off (0).", "secret": False},
            {"key": "AIDON_VM_SUDO_PASSWORD", "label": "VM sudo password", "type": "string", "default": "", "desc": "Sudo password inside the VM, sent on stdin to sudo -S only; never in a command line, log, or error message. Only used when VM sudo is on. Leave empty to keep the saved value unchanged. Default: empty.", "secret": True},
            {"key": "AIDON_MAX_SANDBOX_SECONDS", "label": "Sandbox timeout seconds", "type": "int", "default": "120", "desc": "Timeout for one sandbox command.", "secret": False},
            {"key": "AIDON_SANDBOX_MEM_MB", "label": "Sandbox memory MB", "type": "int", "default": "1024", "desc": "Maximum address space for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_NPROC", "label": "Sandbox process limit", "type": "int", "default": "256", "desc": "Maximum processes for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_FSIZE_MB", "label": "Sandbox file-size MB", "type": "int", "default": "512", "desc": "Maximum file size created by a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_CPU_S", "label": "Sandbox CPU seconds", "type": "int", "default": "0", "desc": "CPU limit for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_BACKEND", "label": "Sandbox backend", "type": "enum", "default": "auto", "desc": "Isolation backend selection.", "secret": False, "options": ["auto", "bubblewrap", "subprocess"]},
            {"key": "AIDON_SANDBOX_NETWORK", "label": "Sandbox network", "type": "bool", "default": "0", "desc": "Allow network access inside sandbox commands.", "secret": False},
            {"key": "AIDON_SANDBOX_REQUIRE_ISOLATION", "label": "Require isolation", "type": "bool", "default": "0", "desc": "Refuse sandbox commands when bubblewrap isolation is unavailable.", "secret": False},
            {"key": "AIDON_KERNEL", "label": "Kernel mode", "type": "bool", "default": "0", "desc": "Kernel mode: write/read/list/shell only, no calculate, no plugins.", "secret": False},
            {"key": "AIDON_TOOL_POSTURE", "label": "Tool posture", "type": "enum", "default": "selfmod", "desc": "How far the self-development ladder is open: locked (seed only, no adoption), standard (pure plugins only), developer (capability plugins: http, fs, run, secrets), selfmod (developer plus source changes shown in full and gated by explicit owner approval). Source changes are limited to existing Python files under src/ and backed up before replacement.", "secret": False, "options": ["locked", "standard", "developer", "selfmod"]},
            {"key": "AIDON_OPTIONAL_TOOLS", "label": "Optional tools", "type": "bool", "default": "0", "desc": "Expose convenience built-ins when enabled. Specialized capabilities remain available when disabled, including AI image editing, document/office and ebook tools, audio transcription/speech, external messaging and publishing, indexed document/feed search, YouTube tools, and schedule management. Their existing approval and credential gates still apply. Webcam photo/video and system-audio recording also remain available, with fresh record approval. Adopted plugins and trusted skills are never gated. Default: off (0).", "secret": False},
            {"key": "AIDON_COMPUTER_DRIVER", "label": "Computer driver", "type": "enum", "default": "", "desc": "Opt-in desktop driver; requires a graphical session and kernel mode off.", "secret": False, "options": ["", "x11"]},
            {"key": "AIDON_COMPUTER_SECRETS", "label": "Computer secrets", "type": "string", "default": "", "desc": "Comma-separated env names the owner authorizes for computer use. Only listed names resolve; the model names a handle, never a value.", "secret": True},
            {"key": "AIDON_SLACK_AUTOSTART", "label": "Slack polling autostart", "type": "bool", "default": "0", "desc": "Start Slack polling automatically with the WebUI.", "secret": False},
            {"key": "AIDON_AUTONOMY_ENABLED", "label": "Autonomy enabled", "type": "bool", "default": "1", "desc": "Trust-based autonomy for repeated skills.", "secret": False},
            {"key": "AIDON_AUTONOMY_THRESHOLD", "label": "Autonomy threshold", "type": "int", "default": "5", "desc": "Successes before a skill graduates a trust tier.", "secret": False},
            {"key": "AIDON_AUTONOMY_DECAY_DAYS", "label": "Autonomy decay days", "type": "string", "default": "14", "desc": "Days after which trust decays.", "secret": False},
            {"key": "AIDON_TIMEZONE", "label": "Timezone", "type": "string", "default": "UTC", "desc": "Timezone for schedules and reports.", "secret": False},
            {"key": "AIDON_MODAL_APP", "label": "Modal app", "type": "string", "default": "aidon-sandbox", "desc": "Modal app name for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_MODAL_CPU", "label": "Modal CPUs", "type": "string", "default": "1.0", "desc": "CPUs per Modal sandbox for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_MODAL_MEMORY_MB", "label": "Modal memory MB", "type": "int", "default": "1024", "desc": "Memory in MB per Modal sandbox for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_ACQ_MAX_TOOLS", "label": "Acquisition max tools", "type": "int", "default": "2", "desc": "Maximum capability tools acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_SKILLS", "label": "Acquisition max skills", "type": "int", "default": "2", "desc": "Maximum skills acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_MCP", "label": "Acquisition max MCP servers", "type": "int", "default": "1", "desc": "Maximum MCP servers acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_DEPS", "label": "Acquisition max dependency sets", "type": "int", "default": "2", "desc": "Maximum dependency sets acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_REPAIRS", "label": "Acquisition max repair cycles", "type": "int", "default": "3", "desc": "Maximum acquisition repair cycles per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_SECONDS", "label": "Acquisition max seconds", "type": "string", "default": "600", "desc": "Maximum seconds of acquisition time per run (soft limit, checked between acquisitions).", "secret": False},
            {"key": "AIDON_HUB_TOKEN", "label": "Hub token", "type": "string", "default": "", "desc": "Token for the capability hub client (aidon hub list/push). Empty = unauthenticated.", "secret": True},
        ]},
        {"group": "Cognitive", "settings": [
            {"key": "AIDON_MODEL", "label": "Chat model", "type": "string", "default": "muse-spark", "desc": "Model id for the agent loop.", "secret": False},
            {"key": "AIDON_BACKEND", "label": "Model backend", "type": "enum", "default": "", "desc": "meta, native, http, or fake.", "secret": False, "options": ["meta", "native", "http", "fake"]},
            {"key": "AIDON_API_ENDPOINT", "label": "API endpoint", "type": "string", "default": "", "desc": "Model API endpoint; leave empty until a backend is selected.", "secret": False},
            {"key": "AIDON_API_KEY", "label": "API key", "type": "string", "default": "", "desc": "Key for the Meta Model API or other endpoint.", "secret": True},
            {"key": "AIDON_FAST_MODEL", "label": "Fast model", "type": "string", "default": "", "desc": "Small model for fast phases, else the main model.", "secret": False},
            {"key": "AIDON_FAST_API_ENDPOINT", "label": "Fast model endpoint", "type": "string", "default": "", "desc": "Endpoint for the fast model; empty uses the main API endpoint.", "secret": False},
            {"key": "AIDON_FAST_API_KEY", "label": "Fast model API key", "type": "string", "default": "", "desc": "API key for the fast model endpoint; empty uses the main API key.", "secret": True},
            {"key": "AIDON_FAST_NUM_CTX", "label": "Fast model context", "type": "int", "default": "0", "desc": "Context window for the fast model, 0 = backend default.", "secret": False},
            {"key": "AIDON_MODEL_THINK", "label": "Model thinking", "type": "bool", "default": "0", "desc": "Enable server-side thinking when supported.", "secret": False},
            {"key": "AIDON_MODEL_NUM_CTX", "label": "Model context", "type": "int", "default": "0", "desc": "Context window for the native backend, 0 = server default.", "secret": False},
            {"key": "AIDON_MODEL_TIMEOUT_S", "label": "Model timeout seconds", "type": "string", "default": "120", "desc": "Timeout for one model call, in seconds.", "secret": False},
            {"key": "AIDON_ROUTER_FAST_PHASES", "label": "Router fast phases", "type": "string", "default": "interpret,learn,respond", "desc": "Comma-separated phases routed to the fast model.", "secret": False},
            {"key": "AIDON_IMAGE_MODEL", "label": "Image model", "type": "string", "default": "muse-image-1.0", "desc": "Image generation model.", "secret": False},
            {"key": "AIDON_SAM_MODEL", "label": "SAM model", "type": "string", "default": "meta-sam", "desc": "Segmentation model for video editing.", "secret": False},
            {"key": "AIDON_SAM_BASE_URL", "label": "SAM base URL", "type": "string", "default": "https://api.meta.ai/v1", "desc": "Meta Model API base URL for SAM.", "secret": False},
            {"key": "AIDON_SAM_API_KEY", "label": "SAM API key", "type": "string", "default": "", "desc": "SAM key; empty uses the main API key.", "secret": True},
            {"key": "AIDON_VERIFIER_MODEL", "label": "Verifier model", "type": "string", "default": "", "desc": "Independent model for verification; empty uses the main model.", "secret": False},
            {"key": "AIDON_VERIFY_SEED", "label": "Verifier seed", "type": "string", "default": "42", "desc": "Deterministic verify seed; leave empty to disable.", "secret": False},
            {"key": "AIDON_VISION_TIERS", "label": "Vision tiers", "type": "string", "default": "strong", "desc": "Comma-separated model tiers allowed to receive images.", "secret": False},
            {"key": "AIDON_THINK_PHASES", "label": "Thinking phases", "type": "string", "default": "interpret,verify", "desc": "Comma-separated phases that enable model thinking; empty disables phase overrides.", "secret": False},
            {"key": "AIDON_DYNAMIC_SAMPLING", "label": "Dynamic sampling", "type": "bool", "default": "1", "desc": "Per-phase temperature/top_p .env overrides apply; 0 = built-in sampling table only.", "secret": False},
            {"key": "AIDON_TEMP_RESPOND", "label": "Temperature (respond)", "type": "string", "default": "", "desc": "Respond-phase temperature override; empty = built-in 0.8.", "secret": False},
            {"key": "AIDON_TOP_P_RESPOND", "label": "Top_p (respond)", "type": "string", "default": "", "desc": "Respond-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_WEBCHAT", "label": "Temperature (webchat)", "type": "string", "default": "", "desc": "Webchat-phase temperature override; empty = built-in 0.2.", "secret": False},
            {"key": "AIDON_TOP_P_WEBCHAT", "label": "Top_p (webchat)", "type": "string", "default": "", "desc": "Webchat-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_PLAN", "label": "Temperature (plan)", "type": "string", "default": "", "desc": "Plan-phase temperature override; empty = built-in 0.2.", "secret": False},
            {"key": "AIDON_TOP_P_PLAN", "label": "Top_p (plan)", "type": "string", "default": "", "desc": "Plan-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_VERIFY", "label": "Temperature (verify)", "type": "string", "default": "", "desc": "Verify-phase temperature override; empty = built-in 0.0.", "secret": False},
            {"key": "AIDON_TOP_P_VERIFY", "label": "Top_p (verify)", "type": "string", "default": "", "desc": "Verify-phase top_p override; empty = built-in 0.9.", "secret": False},
            {"key": "AIDON_EFFORT_RESPOND", "label": "Thinking effort (respond)", "type": "enum", "default": "", "desc": "Respond-phase thinking effort pin; empty = built-in off. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_WEBCHAT", "label": "Thinking effort (webchat)", "type": "enum", "default": "", "desc": "Webchat-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_PLAN", "label": "Thinking effort (plan)", "type": "enum", "default": "", "desc": "Plan-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_VERIFY", "label": "Thinking effort (verify)", "type": "enum", "default": "", "desc": "Verify-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_ROUTER", "label": "Intent router", "type": "enum", "default": "semantic", "desc": "Intent router: semantic (local routing) or clef (Clef decision model; needs AIDON_CLEF_URL). Default: semantic.", "secret": False, "options": ["semantic", "clef"]},
            {"key": "AIDON_CLEF_URL", "label": "Clef URL", "type": "string", "default": "", "desc": "Clef decision-model endpoint URL. Empty = no Clef anywhere.", "secret": False},
            {"key": "AIDON_CLEF_MODEL", "label": "Clef model", "type": "string", "default": "Cloudflare/clef-flash", "desc": "Clef model id.", "secret": False},
            {"key": "AIDON_CLEF_KEY", "label": "Clef key", "type": "string", "default": "", "desc": "Clef proxy-auth key, only when the endpoint requires proxy auth.", "secret": True},
            {"key": "AIDON_CLEF_SECRET", "label": "Clef secret", "type": "string", "default": "", "desc": "Clef proxy-auth secret, only when the endpoint requires proxy auth.", "secret": True},
        ]},
        {"group": "Memory", "settings": [
            {"key": "AIDON_EMBED_MODEL", "label": "Embed model", "type": "string", "default": "google/embeddinggemma-2", "desc": "Embedding model for memory.", "secret": False},
            {"key": "AIDON_EMBEDDER", "label": "Embedder", "type": "enum", "default": "", "desc": "Embedder selection: empty = EmbeddingGemma 2 440M (with AIDON_EMBED_MODEL override), hash = offline hash embedder for tests.", "secret": False, "options": ["", "hash"]},
            {"key": "AIDON_SPARSE_MODEL", "label": "Sparse memory model", "type": "string", "default": "prithivida/Splade_PP_en_v1", "desc": "Learned sparse model for hybrid memory recall; empty uses keyword sparse weights.", "secret": False},
            {"key": "AIDON_EMBEDDING_REVISION", "label": "Embed model revision", "type": "string", "default": "f9972ac22e696a105c482202646979e33e4b5a1e", "desc": "Pinned snapshot revision of the embedding model. Upstream README edits otherwise mint a new snapshot directory on every HEAD move, which looks like the weights downloading again. Override with another revision hash to use a different snapshot. Default: f9972ac22e696a105c482202646979e33e4b5a1e.", "secret": False},
            {"key": "AIDON_FORGET_HALF_LIFE_DAYS", "label": "Forget half-life days", "type": "string", "default": "21", "desc": "Forgetting-curve half-life, in days.", "secret": False},
            {"key": "AIDON_FORGET_GRACE_DAYS", "label": "Forget grace days", "type": "int", "default": "35", "desc": "Days before forgetting starts.", "secret": False},
            {"key": "AIDON_FORGET_CLEANUP_THRESHOLD", "label": "Forget cleanup threshold", "type": "string", "default": "0.02", "desc": "Recall weight below which a point is pruned.", "secret": False},
            {"key": "AIDON_EPISODE_RECALL_LIMIT", "label": "Episode recall limit", "type": "int", "default": "1", "desc": "Number of prior experiences considered before a fresh plan. Composition always uses at least 5; that floor is fixed and cannot go lower.", "secret": False},
            {"key": "AIDON_KNOWLEDGE_RECALL_LIMIT", "label": "Knowledge recall limit", "type": "int", "default": "3", "desc": "Number of knowledge points considered before planning.", "secret": False},
            {"key": "AIDON_RECALL_MAX_TOKENS", "label": "Recall token budget", "type": "int", "default": "2000", "desc": "Maximum memory context included in a plan.", "secret": False},
            {"key": "AIDON_LEARN_MAX_SKILLS_PER_DAY", "label": "Learning skills per day", "type": "int", "default": "5", "desc": "Maximum candidate skills promoted by one learn cycle per day.", "secret": False},
            {"key": "AIDON_SKILL_MAX_FAILURES", "label": "Skill failure limit", "type": "int", "default": "3", "desc": "Failures before a skill is retired.", "secret": False},
            {"key": "AIDON_INGEST_DIR", "label": "Ingest directory", "type": "string", "default": "", "desc": "Folder watched for ingest triggers.", "secret": False},
            {"key": "AIDON_QDRANT_URL", "label": "Qdrant URL", "type": "string", "default": "", "desc": "Qdrant server URL, empty = local mode.", "secret": False},
            {"key": "AIDON_MEMORY_EXTRACT", "label": "Background fact extraction", "type": "int", "default": "1", "desc": "Extract facts and understandings in the background after chats and runs; 0 disables it.", "secret": False},
            {"key": "AIDON_DAG_SIMILARITY", "label": "DAG similarity threshold", "type": "string", "default": "0.3", "desc": "Goal-shape overlap (0-1) for DAG replacement, experience adaptation, and failure matching; lower merges more aggressively.", "secret": False},
            {"key": "AIDON_LEARN_MAX_TOKENS_PER_DAY", "label": "Learn tokens per day", "type": "int", "default": "500000", "desc": "Tokens the self-improvement loop may spend per UTC day (learn phase only). Default: 500000.", "secret": False},
            {"key": "AIDON_SEMANTIC_RECALL_LIMIT", "label": "Semantic recall limit", "type": "int", "default": "3", "desc": "General-fact (semantic) recall limit for plan prompts. Default: 3.", "secret": False},
            {"key": "AIDON_RECALL_CAP_EPISODES", "label": "Recall cap episodes", "type": "int", "default": "700", "desc": "Per-source cap for episode recall in the fused planning block; one noisy source can't starve the others. Default: 700.", "secret": False},
            {"key": "AIDON_RECALL_CAP_KNOWLEDGE", "label": "Recall cap knowledge", "type": "int", "default": "700", "desc": "Per-source cap for knowledge recall in the fused planning block. Default: 700.", "secret": False},
            {"key": "AIDON_RECALL_CAP_SKILLS", "label": "Recall cap skills", "type": "int", "default": "500", "desc": "Per-source cap for skill recall in the fused planning block. Default: 500.", "secret": False},
        ]},
        {"group": "Voice", "settings": [
            {"key": "AIDON_TTS_VOICE_ID", "label": "TTS voice id", "type": "string", "default": "", "desc": "Voice id for text-to-speech.", "secret": False},
            {"key": "AIDON_VOICE_ANNOUNCE", "label": "Voice announcements", "type": "bool", "default": "0", "desc": "Automatically play spoken WebUI replies.", "secret": False},
            {"key": "AIDON_FISH_API_KEY", "label": "Fish API key", "type": "string", "default": "", "desc": "Key for Fish TTS.", "secret": True},
            {"key": "AIDON_TRANSCRIBE_API_KEY", "label": "Transcribe API key", "type": "string", "default": "", "desc": "Key for voice transcription.", "secret": True},
        ]},
        {"group": "Agentic", "settings": [
            {"key": "AIDON_MAX_TOKENS_PER_RUN", "label": "Max tokens per run", "type": "int", "default": "200000", "desc": "Hard token budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_STEPS", "label": "Max steps", "type": "int", "default": "50", "desc": "Hard step budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_WALLCLOCK_MIN", "label": "Max wallclock minutes", "type": "int", "default": "60", "desc": "Hard wallclock budget for one goal run, in minutes.", "secret": False},
            {"key": "AIDON_PARALLEL_WORKERS", "label": "Parallel DAG workers", "type": "int", "default": "3", "desc": "Max concurrent DAG/plan steps in one level; 1 = sequential.", "secret": False},
            {"key": "AIDON_MAX_STUCK_ITERATIONS", "label": "Max stuck iterations", "type": "int", "default": "8", "desc": "Iterations without progress before the run stops.", "secret": False},
            {"key": "AIDON_PARALLEL_TOOLS", "label": "Parallel tools", "type": "int", "default": "4", "desc": "Parallel research/tool workers.", "secret": False},
            {"key": "AIDON_MAX_ATTEMPTS", "label": "Repair attempts per run", "type": "int", "default": "3", "desc": "Execute, verify, repair, up to this many tries.", "secret": False},
            {"key": "AIDON_MAX_REPLANS", "label": "Research-plus-replan passes", "type": "int", "default": "1", "desc": "Replan passes before execution; 0 skips replanning.", "secret": False},
            {"key": "AIDON_MAX_PLAN_REPAIRS", "label": "Plan repair tries", "type": "int", "default": "2", "desc": "Repair tries when the validator rejects a plan; 0 fails immediately.", "secret": False},
            {"key": "AIDON_REACT_MAX_ROUNDS", "label": "ReAct max rounds", "type": "int", "default": "8", "desc": "Max rounds for the ReAct compatibility loop.", "secret": False},
            {"key": "AIDON_REACT_MAX_WORKERS", "label": "ReAct parallel workers", "type": "int", "default": "3", "desc": "Parallel tool workers within one ReAct round; 1 = sequential.", "secret": False},
            {"key": "AIDON_CRITIC_ENABLED", "label": "Plan critic on/off", "type": "int", "default": "1", "desc": "Reflection pass on each valid plan before execution; 0 skips it.", "secret": False},
            {"key": "AIDON_CRITIC_MAX_ROUNDS", "label": "Critic repair rounds", "type": "int", "default": "1", "desc": "Critic-issue repair rounds; 0 runs the critic without repairing.", "secret": False},
            {"key": "AIDON_CRITIC_MAX_ISSUES", "label": "Critic issues per round", "type": "int", "default": "5", "desc": "Most issues the critic returns per review round; repairs address them in order.", "secret": False},
            {"key": "AIDON_CRITIC_MODE", "label": "Critic mode", "type": "enum", "default": "risk", "desc": "Plan critic scope: risk (review only fresh large plans and plans with live steps; adapted and small plans skip it) or always (review every valid plan). Flip to always if risk-based review misses real plan flaws. Default: risk.", "secret": False, "options": ["risk", "always"]},
            {"key": "AIDON_CRITIC_RISK_MAX_STEPS", "label": "Critic risk small-plan steps", "type": "int", "default": "3", "desc": "A plan with at most this many steps counts as small for risk-based critic mode and skips review when it has no live steps; 0 means only adapted plans skip. Default: 3.", "secret": False},
            {"key": "AIDON_MODEL_VERIFIER", "label": "Model verifier mode", "type": "enum", "default": "off", "desc": "Model second opinion after code verification passes: off, selective (includes HTML and prose files), or always. It reviews file content; it does not execute code or prove browser behavior. Default: off.", "secret": False, "options": ["off", "selective", "always"]},
            {"key": "AIDON_CHOICE_POLICY", "label": "Choice policy", "type": "enum", "default": "auto", "desc": "Choice policy when research finds real alternatives: auto (reason and pick) or ask (present ranked candidates at a choice gate). Default: auto.", "secret": False, "options": ["auto", "ask"]},
        ]},
        {"group": "Social", "settings": [
            {"key": "AIDON_WHATSAPP_TOKEN", "label": "WhatsApp token", "type": "string", "default": "", "desc": "WhatsApp Cloud API token.", "secret": True},
            {"key": "AIDON_WHATSAPP_PHONE_ID", "label": "WhatsApp phone id", "type": "string", "default": "", "desc": "WhatsApp Cloud phone number id.", "secret": False},
            {"key": "AIDON_WHATSAPP_VERIFY_TOKEN", "label": "WhatsApp verify token", "type": "string", "default": "", "desc": "Token WhatsApp uses to verify the webhook.", "secret": True},
            {"key": "AIDON_WHATSAPP_APP_SECRET", "label": "WhatsApp app secret", "type": "string", "default": "", "desc": "App secret for WhatsApp signature checks.", "secret": True},
            {"key": "AIDON_WHATSAPP_OWNER", "label": "WhatsApp owner", "type": "string", "default": "", "desc": "Owner phone number for the WhatsApp owner gate.", "secret": False},
            {"key": "SLACK_BOT_TOKEN", "label": "Slack bot token", "type": "string", "default": "", "desc": "Slack bot token for inbound/outbound Slack polling.", "secret": True},
            {"key": "AIDON_SLACK_SIGNING_SECRET", "label": "Slack signing secret", "type": "string", "default": "", "desc": "Required for the Slack webhook; requests are rejected when it is empty.", "secret": True},
            {"key": "AIDON_THREADS_ACCOUNT_ID", "label": "Threads account id", "type": "string", "default": "", "desc": "Threads account id for publishing.", "secret": False},
            {"key": "AIDON_THREADS_ACCESS_TOKEN", "label": "Threads access token", "type": "string", "default": "", "desc": "Threads access token for publishing.", "secret": True},
            {"key": "AIDON_IG_USER_ID", "label": "Instagram user id", "type": "string", "default": "", "desc": "Instagram user id for publishing.", "secret": False},
            {"key": "AIDON_IG_ACCESS_TOKEN", "label": "Instagram access token", "type": "string", "default": "", "desc": "Instagram access token for publishing.", "secret": True},
            {"key": "GITHUB_TOKEN", "label": "GitHub token", "type": "string", "default": "", "desc": "GitHub token for repository tools.", "secret": True},
            {"key": "AIDON_SLACK_INBOUND_CHANNEL", "label": "Slack inbound channel(s)", "type": "string", "default": "", "desc": "Comma-separated Slack channel or DM IDs for polling.", "secret": False},
            {"key": "AIDON_SLACK_OUTBOUND_CHANNEL", "label": "Slack outbound channel", "type": "string", "default": "", "desc": "Slack channel outbound replies go to; empty = the inbound channel.", "secret": False},
            {"key": "AIDON_SLACK_POLL_SECONDS", "label": "Slack poll seconds", "type": "int", "default": "10", "desc": "Polling interval for Slack inbound messages.", "secret": False},
            {"key": "AIDON_SLACK_ACK_AFTER_SECONDS", "label": "Slack acknowledgement delay", "type": "int", "default": "15", "desc": "Seconds before a long-running Slack goal gets an acknowledgement.", "secret": False},
            {"key": "AIDON_OWNER_EMAIL", "label": "Owner email", "type": "string", "default": "", "desc": "Where drafts and reports to the owner are addressed.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_CHANNEL", "label": "Approval nudge channel", "type": "string", "default": "", "desc": "Channel approval nudges go to.", "secret": False},
            {"key": "AIDON_CORRECTED_SUBJECT", "label": "Corrected subject", "type": "bool", "default": "1", "desc": "Mark corrected emails with a Corrected subject.", "secret": False},
        ]},
        {"group": "Research", "settings": [
            {"key": "AIDON_SEARCH_BACKEND", "label": "Search backend", "type": "enum", "default": "tavily", "desc": "Web search backend.", "secret": False, "options": ["tavily", "searxng"]},
            {"key": "AIDON_SEARXNG_URL", "label": "SearXNG URL", "type": "string", "default": "http://localhost:8888", "desc": "SearXNG server for the searxng backend.", "secret": False},
            {"key": "TAVILY_API_KEY", "label": "Tavily API key", "type": "string", "default": "", "desc": "Key for Tavily search.", "secret": True},
            {"key": "AIDON_RESEARCH_MAX_ROUNDS", "label": "Research max rounds", "type": "int", "default": "10", "desc": "Max research rounds per run.", "secret": False},
            {"key": "AIDON_RESEARCH_ALLOWLIST", "label": "Research allowlist", "type": "string", "default": "", "desc": "Comma-separated domains research may use, empty = unrestricted.", "secret": False},
            {"key": "AIDON_YOUTUBE_API_KEY", "label": "YouTube API key", "type": "string", "default": "", "desc": "Key for YouTube search.", "secret": True},
            {"key": "YOUTUBE_CLIENT_ID", "label": "YouTube OAuth client ID", "type": "string", "default": "", "desc": "OAuth client ID for YouTube uploads (youtube.upload scope).", "secret": True},
            {"key": "YOUTUBE_CLIENT_SECRET", "label": "YouTube OAuth client secret", "type": "string", "default": "", "desc": "OAuth client secret for YouTube uploads.", "secret": True},
            {"key": "YOUTUBE_REFRESH_TOKEN", "label": "YouTube OAuth refresh token", "type": "string", "default": "", "desc": "Refresh token from a one-time youtube.upload consent.", "secret": True},
            {"key": "YOUTUBE_PRIVACY_STATUS", "label": "YouTube upload privacy", "type": "enum", "default": "private", "desc": "Default visibility for YouTube uploads.", "secret": False, "options": ["private", "unlisted", "public"]},
            {"key": "YOUTUBE_CATEGORY_ID", "label": "YouTube upload category", "type": "string", "default": "22", "desc": "Category ID for YouTube uploads (22 = People & Blogs).", "secret": False},
            {"key": "AIDON_DEEP_RESEARCH_ROUNDS", "label": "Deep research rounds", "type": "int", "default": "2", "desc": "Max search rounds for the deep_research tool.", "secret": False},
            {"key": "AIDON_DEEP_RESEARCH_SOURCES", "label": "Deep research sources", "type": "int", "default": "5", "desc": "Max pages fetched per deep_research round.", "secret": False},
        ]},
    ]
    _CATALOG_FLOAT_KEYS = frozenset({
        "AIDON_MODEL_TIMEOUT_S", "AIDON_FORGET_HALF_LIFE_DAYS",
        "AIDON_FORGET_CLEANUP_THRESHOLD", "AIDON_AUTONOMY_DECAY_DAYS",
        "AIDON_DAG_SIMILARITY", "AIDON_MODAL_CPU", "AIDON_ACQ_MAX_SECONDS",
    })

    def _catalog_entry(self, env_key: str):
        for group in self.SETTINGS_CATALOG:
            for entry in group["settings"]:
                if entry["key"] == env_key:
                    return entry
        return None

    def _read_env_file(self) -> dict:
        """Parse <home>/.env into {key: value}, no env fallback here."""
        path = self.home / ".env"
        values: dict[str, str] = {}
        if not path.is_file():
            return values
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            key, value = stripped.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
        return values

    def settings_catalog(self) -> dict:
        """Catalog + current values. Secrets come back masked.

    A secret entry returns a fixed ``****`` mask when set and
    is_set, never the value itself. A non-secret returns its .env value, else
        its process-environment value, else its default, with
        from_file saying whether .env set it.
        """
        import os
        # Only expose a slider when the bound is meaningful and safe for
        # the setting.  Unbounded or deployment-specific values remain
        # ordinary inputs in the Settings page.
        bounds = {
            "AIDON_FAST_NUM_CTX": (0, 262144, 1024),
            "AIDON_MODEL_NUM_CTX": (0, 262144, 1024),
            "AIDON_MAX_TOKENS_PER_RUN": (1000, 1000000, 1000),
            "AIDON_MAX_STEPS": (1, 200, 1),
            "AIDON_MAX_WALLCLOCK_MIN": (1, 1440, 1),
            "AIDON_APPROVAL_TIMEOUT_S": (30, 3600, 30),
            "AIDON_MAX_SANDBOX_SECONDS": (1, 3600, 1),
            "AIDON_SANDBOX_MEM_MB": (0, 32768, 128),
            "AIDON_SANDBOX_NPROC": (0, 4096, 1),
            "AIDON_SANDBOX_FSIZE_MB": (0, 102400, 512),
            "AIDON_SANDBOX_CPU_S": (0, 86400, 60),
            "AIDON_FORGET_GRACE_DAYS": (0, 3650, 1),
            "AIDON_EPISODE_RECALL_LIMIT": (0, 100, 1),
            "AIDON_KNOWLEDGE_RECALL_LIMIT": (0, 100, 1),
            "AIDON_RECALL_MAX_TOKENS": (0, 100000, 100),
            "AIDON_LEARN_MAX_SKILLS_PER_DAY": (0, 100, 1),
            "AIDON_SKILL_MAX_FAILURES": (0, 100, 1),
            "AIDON_RESEARCH_MAX_ROUNDS": (0, 100, 1),
            "AIDON_DEEP_RESEARCH_ROUNDS": (1, 5, 1),
            "AIDON_DEEP_RESEARCH_SOURCES": (1, 10, 1),
            "AIDON_MAX_STUCK_ITERATIONS": (0, 100, 1),
            "AIDON_PARALLEL_TOOLS": (1, 32, 1),
            "AIDON_PARALLEL_WORKERS": (1, 32, 1),
            "AIDON_MAX_ATTEMPTS": (1, 100, 1),
            "AIDON_MAX_REPLANS": (0, 10, 1),
            "AIDON_MAX_PLAN_REPAIRS": (0, 10, 1),
            "AIDON_REACT_MAX_ROUNDS": (1, 100, 1),
            "AIDON_REACT_MAX_WORKERS": (1, 32, 1),
            "AIDON_CRITIC_ENABLED": (0, 1, 1),
            "AIDON_CRITIC_MAX_ROUNDS": (0, 5, 1),
            "AIDON_CRITIC_MAX_ISSUES": (1, 20, 5),
            "AIDON_CRITIC_RISK_MAX_STEPS": (0, 20, 3),
            "AIDON_MEMORY_EXTRACT": (0, 1, 1),
            "AIDON_SLACK_POLL_SECONDS": (3, 3600, 1),
            "AIDON_SLACK_ACK_AFTER_SECONDS": (0, 3600, 1),
            "AIDON_APPROVAL_NUDGE_MIN": (0, 1440, 1),
            "AIDON_AUTONOMY_THRESHOLD": (0, 100, 1),
            "AIDON_WEBUI_PORT": (1024, 65535, 1),
        }
        file_vals = self._read_env_file()
        groups = []
        for group in self.SETTINGS_CATALOG:
            items = []
            for entry in group["settings"]:
                item = {k: entry[k] for k in
                        ("key", "label", "type", "default", "desc", "secret")
                        if k in entry}
                if "options" in entry:
                    item["options"] = list(entry["options"])
                if entry.get("type") == "int" and entry["key"] in bounds:
                    item["min"], item["max"], item["step"] = bounds[entry["key"]]
                raw = file_vals.get(entry["key"])
                if raw is None:
                    raw = os.environ.get(entry["key"])
                item["from_file"] = entry["key"] in file_vals
                if entry.get("secret"):
                    item["is_set"] = bool(raw)
                    item["value"] = "****" if item["is_set"] else ""
                else:
                    item["value"] = raw if raw is not None else entry.get("default", "")
                items.append(item)
            groups.append({"group": group["group"], "settings": items})
        return {"env_file": str(self.home / ".env"), "groups": groups}

    def settings_set_env(self, key: str, value) -> dict:
        """Write one setting to <home>/.env, secrets included.

        The settings page can enter
        everything, keys included. Accepts the friendly settings
        name (e.g. "timezone") or the env var name itself (e.g.
        "AIDON_TIMEZONE"), for every catalog key plus the original
        19 friendly names. Type validation: int must parse as int,
        bool must be one of 0/1/true/false/yes/no (stored as 1/0),
        enum must be in options, float-valued keys must parse as a
        number. A secret key with an empty value is a no-op
        ("don't change") and must never clear the key; a secret
        with a value is written, write-only — the catalog never
        returns it back (value "****" plus is_set when set). Unknown keys and
        newline values are refused. The value is never printed or
        returned: the result carries only the key name.
        The .env file is created if missing and kept owner-only.
        """
        raw_key = str(key or "").strip()
        env_key = self._ENV_SETTING_MAP.get(raw_key) or raw_key
        entry = self._catalog_entry(env_key)
        if entry is None and env_key not in self._ENV_SETTING_MAP.values():
            return {"ok": False, "error": f"setting not writable to .env: {raw_key}"}
        is_secret = bool(entry is not None and entry.get("secret")) or any(
            marker in env_key for marker in self._ENV_SECRET_MARKERS)
        if is_secret and str(value) == "":
            return {"ok": True, "key": env_key, "unchanged": True}
        text = str(value).strip() if not isinstance(value, str) else value.strip()
        if "\n" in text or "\r" in text:
            return {"ok": False, "error": "value must not contain a newline"}
        if entry is not None:
            kind = entry.get("type", "string")
            if kind == "int":
                try:
                    text = str(int(text))
                except ValueError:
                    return {"ok": False, "error": f"{env_key}: not an integer: {value!r}"}
                if env_key == "AIDON_ACQ_MAX_TOOLS" and int(text) < 0:
                    return {"ok": False,
                            "error": f"{env_key}: must be nonnegative"}
            elif kind == "bool":
                lowered = text.lower()
                if lowered not in ("0", "1", "true", "false", "yes", "no"):
                    return {"ok": False, "error": f"{env_key}: not a boolean: {value!r}"}
                text = "1" if lowered in ("1", "true", "yes") else "0"
            elif kind == "enum":
                if text not in (entry.get("options") or []):
                    return {"ok": False, "error": f"{env_key}: must be one of {entry.get('options')}"}
            elif env_key in self._CATALOG_FLOAT_KEYS:
                try:
                    parsed_float = float(text)
                except ValueError:
                    return {"ok": False, "error": f"{env_key}: not a number: {value!r}"}
                if not math.isfinite(parsed_float):
                    return {"ok": False,
                            "error": f"{env_key}: must be a finite number: {value!r}"}
                if (env_key == "AIDON_ACQ_MAX_SECONDS"
                        and parsed_float < 0):
                    return {"ok": False,
                            "error": f"{env_key}: must be nonnegative"}
        path = self.home / ".env"
        lines: list[str] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        out: list[str] = []
        found = False
        for line in lines:
            stripped = line.strip()
            candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
            if "=" in candidate and candidate.split("=", 1)[0].strip() == env_key and not found:
                out.append(f"{env_key}={text}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"{env_key}={text}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return {"ok": True, "key": env_key}

