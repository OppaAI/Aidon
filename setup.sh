#!/bin/sh
# Aidon setup: first-time setup, idempotent, run it twice safely.
#
# Interactive by default, like the old Aidon setup: it asks for
# things one by one, and every answer is skippable.
#   - A model backend first: Meta Model API (hackathon) or Ollama
#     (local, no Meta key) or another OpenAI-compatible endpoint.
#   - Then model names, one by one: Enter keeps the shown default,
#     typing replaces it.
#   - Then API keys, one by one: a key already set is never asked
#     again, an empty answer skips that feature (it stays off, and
#     can be added later in .env or on the WebUI /settings page).
#   - Then optional packages, one by one: each package is offered
#     with a yes/no, and only the ones you say yes to are installed.
#     A missing optional package is not an error: the feature that
#     needs it simply stays off until it is installed.
#
# Non-interactive: ./setup.sh --non-interactive
#   No prompts. Copies examples/.env.example to $AIDON_HOME/.env
#   only if it does not already exist, never overwriting it.
# Install every optional package without asking:
#   ./setup.sh --with-optional
#
# What it does, besides asking:
# - Checks python3, reports the version, does not install python.
# - Creates the Aidon home (AIDON_HOME, or ~/.aidon by default)
#   with workspace, ingest, plugins, skills, specs, and
#   library/files directories.
# - Checks bubblewrap and Docker, reports what is available.
#   It does NOT install Docker, and it does NOT install a VM.
#   Docker is installed by you, on the machine where Aidon runs,
#   if you want the Docker backend. A VM is post-hackathon,
#   future only, not installed here.
# - Secrets live in $AIDON_HOME/.env only, never in chat, source,
#   or skills. Setup never prints a key back.
#
# Usage: ./setup.sh [--non-interactive] [--with-optional]
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AIDON_HOME=${AIDON_HOME:-$HOME/.aidon}
NONINTERACTIVE=0
WITH_OPTIONAL=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive) NONINTERACTIVE=1 ;;
    --with-optional) WITH_OPTIONAL=1 ;;
    -h|--help)
      echo "usage: ./setup.sh [--non-interactive] [--with-optional]"
      exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

echo "Aidon home: $AIDON_HOME"
mkdir -p "$AIDON_HOME/workspace" "$AIDON_HOME/ingest" "$AIDON_HOME/plugins" "$AIDON_HOME/skills" "$AIDON_HOME/specs" "$AIDON_HOME/library/files" "$AIDON_HOME/runs" "$AIDON_HOME/summaries" "$AIDON_HOME/learned" "$AIDON_HOME/email/drafts" "$AIDON_HOME/trace" "$AIDON_HOME/v1" "$AIDON_HOME/live" "$AIDON_HOME/dags"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed. Install Python 3.11 or later, then run ./setup.sh again." >&2
  exit 1
fi
echo "python3: $(python3 --version 2>&1)"

if [ -f "$AIDON_HOME/.env" ]; then
  echo ".env already exists at $AIDON_HOME/.env, not overwriting it."
else
  if [ -f "$REPO_DIR/examples/.env.example" ]; then
    cp "$REPO_DIR/examples/.env.example" "$AIDON_HOME/.env"
    echo "Created $AIDON_HOME/.env from examples/.env.example."
  else
    echo "WARNING: examples/.env.example not found, no .env created." >&2
  fi
fi

dotenv_get() {
  DOTENV_FILE="$AIDON_HOME/.env" DOTENV_KEY="$1" python3 -c "
import os
key = os.environ['DOTENV_KEY']
try:
    for line in open(os.environ['DOTENV_FILE'], encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        k, v = line.split('=', 1)
        if k.strip() == key:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in (\"'\", '\"'):
                v = v[1:-1]
            print(v)
            break
except OSError:
    pass
" 2>/dev/null || true
}
dotenv_set() {
  DOTENV_FILE="$AIDON_HOME/.env" DOTENV_KEY="$1" DOTENV_VAL="$2" python3 -c "
import os
path = os.environ['DOTENV_FILE']
key, val = os.environ['DOTENV_KEY'], os.environ['DOTENV_VAL']
lines = []
try:
    lines = open(path, encoding='utf-8').read().splitlines()
except OSError:
    lines = []
out = []
found = False
for line in lines:
    stripped = line.strip()
    k = stripped[5:].strip() if stripped.startswith('export ') else stripped
    if '=' in k and k.split('=', 1)[0].strip() == key and not found:
        out.append(f'{key}={val}')
        found = True
    else:
        out.append(line)
if not found:
    out.append(f'{key}={val}')
open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
"
}
ask_key() {
  if [ "$NONINTERACTIVE" = 1 ]; then return 0; fi
  cur=$(dotenv_get "$1")
  if [ -n "$cur" ]; then echo "$1: already set"; return 0; fi
  val=""
  printf '%s (empty = skip): ' "$2" >&2
  read -r val || true
  [ -z "$val" ] && return 0
  dotenv_set "$1" "$val"
}
ask_setting() {
  if [ "$NONINTERACTIVE" = 1 ]; then return 0; fi
  cur=$(dotenv_get "$1")
  if [ -z "$cur" ]; then cur="${3:-}"; fi
  val=""
  printf '%s [%s]: ' "$2" "$cur" >&2
  read -r val || true
  [ -z "$val" ] && val="$cur"
  dotenv_set "$1" "$val"
}
ask_yes_no() {
  if [ "$NONINTERACTIVE" = 1 ]; then return 1; fi
  yn=""
  printf '%s [y/N]: ' "$1" >&2
  read -r yn || true
  case "${yn:-n}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}
install_package() {
  if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install "$1" || echo "  install of $1 failed"
  elif command -v uv >/dev/null 2>&1; then
    uv pip install "$1" || echo "  install of $1 failed"
  else
    echo "  no pip and no uv found, install $1 yourself, then rerun ./setup.sh"
  fi
}

if [ "$NONINTERACTIVE" = 0 ]; then
  echo ""
  echo "Model backend - Aidon needs an LLM, and there are two ways in:"
  echo "  1) Meta Model API (the hackathon API, needs a key)"
  echo "  2) Ollama (local, no Meta key - the alternative when you do"
  echo "     not use the hackathon API; Ollama must be running and the"
  echo "     model must be pulled)"
  echo "  3) Another OpenAI-compatible endpoint"
  cur_backend=$(dotenv_get "AIDON_BACKEND")
  cur_endpoint=$(dotenv_get "AIDON_API_ENDPOINT")
  default_choice=1
  if [ "$cur_backend" = "ollama" ]; then default_choice=2; fi
  case "$cur_endpoint" in
    *localhost:11434*|*127.0.0.1:11434*) default_choice=2 ;;
  esac
  choice=""
  printf 'Choose a backend [1/2/3, default %s]: ' "$default_choice" >&2
  read -r choice || true
  [ -z "$choice" ] && choice="$default_choice"
  case "$choice" in
    2)
      dotenv_set "AIDON_BACKEND" "ollama"
      echo "Ollama: no Meta API key needed."
      # A Meta model name left over from .env.example is not a local
      # model; clear it so Enter does not keep it by accident.
      case "$(dotenv_get "AIDON_MODEL")" in
        muse-spark*) dotenv_set "AIDON_MODEL" "" ;;
      esac
      ask_setting "AIDON_OLLAMA_ENDPOINT" "Ollama endpoint" "http://localhost:11434"
      ask_setting "AIDON_MODEL" "Ollama model (must be pulled in Ollama)"
      ask_setting "AIDON_FAST_MODEL" "Fast-tier model (empty = no routing)"
      model=$(dotenv_get "AIDON_MODEL")
      if command -v ollama >/dev/null 2>&1; then
        if [ -n "$model" ] && ask_yes_no "Pull $model now with ollama pull?"; then
          ollama pull "$model" || echo "  ollama pull failed, pull it yourself."
        fi
      else
        echo "Ollama is not on PATH. Install it, start it, and pull the model above,"
        echo "or Aidon cannot call an LLM until then."
      fi
      ;;
    3)
      dotenv_set "AIDON_BACKEND" "http"
      ask_setting "AIDON_API_ENDPOINT" "Model API endpoint"
      ask_setting "AIDON_MODEL" "LLM model"
      ask_setting "AIDON_FAST_MODEL" "Fast-tier model (empty = no routing)"
      ask_key "AIDON_API_KEY" "Model API key"
      ;;
    *)
      dotenv_set "AIDON_BACKEND" "meta"
      ask_setting "AIDON_API_ENDPOINT" "Meta Model API endpoint" "https://api.meta.ai/v1"
      ask_setting "AIDON_MODEL" "LLM model" "muse-spark-1.3"
      ask_setting "AIDON_FAST_MODEL" "Fast-tier model (empty = no routing)"
      ask_key "AIDON_API_KEY" "Meta Model API key"
      backend_key=$(dotenv_get "AIDON_API_KEY")
      if [ -z "$backend_key" ]; then
        echo "WARNING: Meta backend selected but no Meta API key is set;"
        echo "Aidon cannot call an LLM until AIDON_API_KEY is added."
      fi
      ;;
  esac
  echo ""
  echo "Embeddings - changing this later needs a fresh memory store."
  ask_setting "AIDON_EMBED_MODEL" "Embedding model" "google/embeddinggemma-2"
  ask_setting "AIDON_RESEARCH_ALLOWLIST" "Research allowlist, comma domains (empty = unrestricted)"
  echo ""
  echo "API keys - each one enables one optional feature, and each can"
  echo "be added later in .env or on the /settings page."
  ask_key "TAVILY_API_KEY" "Tavily API key (web search; empty = SearXNG)"
  ask_key "AIDON_FISH_API_KEY" "Fish Audio API key (voice playback)"
  ask_key "SLACK_BOT_TOKEN" "Slack bot token (xoxb-*)"
  ask_key "SLACK_SIGNING_SECRET" "Slack signing secret"
  ask_key "AIDON_WHATSAPP_TOKEN" "WhatsApp token"
  ask_key "AIDON_WHATSAPP_PHONE_ID" "WhatsApp phone ID"
  ask_key "AIDON_WHATSAPP_VERIFY_TOKEN" "WhatsApp verify token"
  ask_key "AIDON_WHATSAPP_APP_SECRET" "WhatsApp app secret"
  ask_key "AIDON_WHATSAPP_OWNER" "WhatsApp owner number"
  ask_key "GMAIL_CLIENT_ID" "Gmail client ID"
  ask_key "GMAIL_CLIENT_SECRET" "Gmail client secret"
  ask_key "GMAIL_REFRESH_TOKEN" "Gmail refresh token"
  ask_key "AIDON_YOUTUBE_API_KEY" "YouTube API key"
  ask_key "AIDON_THREADS_ACCESS_TOKEN" "Threads access token"
  ask_key "AIDON_IG_ACCESS_TOKEN" "Instagram access token"
  ask_key "GITHUB_TOKEN" "GitHub token"
  ask_key "AIDON_TRANSCRIBE_API_KEY" "Transcribe API key"
  ask_key "AIDON_WEBUI_TOKEN" "WebUI token (empty = localhost only)"
  echo "keeping secrets in .env only - never in chat, source, or skills."
fi

if command -v bwrap >/dev/null 2>&1; then
  echo "bubblewrap: available, sandbox backend is isolated."
else
  echo "bubblewrap: not installed, sandbox fallback runs in the workdir and is labeled not isolated."
  if [ "$NONINTERACTIVE" = 0 ] && command -v apt-get >/dev/null 2>&1; then
    if ask_yes_no "Install bubblewrap now via apt (needs sudo)?"; then
      sudo apt-get install -y bubblewrap || echo "  bubblewrap install failed, install it yourself."
    fi
  fi
fi

if command -v docker >/dev/null 2>&1; then
  echo "Docker: available ($(docker --version 2>&1 | head -n 1)). Set AIDON_EXECUTION_BACKEND=docker in .env to use it."
else
  echo "Docker: not installed. Docker is needed only if you want the Docker backend."
  echo "  macOS: install Docker Desktop, or: brew install --cask docker"
  echo "  Ubuntu: sudo apt install docker.io, then: sudo usermod -aG docker \$USER, then log out and back in."
  echo "  Windows: install Docker Desktop."
  echo "  Until Docker is installed, use the bubblewrap sandbox backend, which is the default."
fi

echo "VM: not installed, and not installed by setup.sh. A VM is post-hackathon, future only."

echo ""
echo "Optional packages - each one enables one feature, and each is"
echo "offered on its own. A package you skip is not installed, and"
echo "its feature simply stays off until you install it."
for spec in \
  "qdrant-client:qdrant_client:Qdrant vector memory" \
  "sentence-transformers:sentence_transformers:real semantic embeddings" \
  "fastembed:fastembed:learned SPLADE sparse recall" \
  "Pillow:PIL:image handling and video annotation" \
  "pypdf:pypdf:PDF reading" \
  "python-docx:docx:Word reading" \
  "beautifulsoup4:bs4:HTML reading" \
  "ebooklib:ebooklib:EPUB building" \
  "markdown:markdown:EPUB building" \
  "markitdown:markitdown:universal document reading" \
  ; do
  pkg=$(printf '%s' "$spec" | cut -d: -f1)
  mod=$(printf '%s' "$spec" | cut -d: -f2)
  what=$(printf '%s' "$spec" | cut -d: -f3)
  if python3 -c "import $mod" 2>/dev/null; then
    echo "optional package $pkg: present ($what)"
  else
    echo "optional package $pkg: missing ($what)"
    if [ "$WITH_OPTIONAL" = 1 ]; then
      echo "  installing $pkg (--with-optional)..."
      install_package "$pkg"
    elif [ "$NONINTERACTIVE" = 0 ] && ask_yes_no "Install $pkg ($what)?"; then
      install_package "$pkg"
    else
      echo "  skipped, $what stays off until $pkg is installed."
    fi
  fi
done

echo ""
echo "Setup done. Next: fill in anything you skipped in $AIDON_HOME/.env, then ./launch.sh"
echo ""
echo "Keys and setup, by you, after this script, names only, no values here:"
echo "  Model: AIDON_BACKEND (meta | ollama | http | fake), AIDON_API_ENDPOINT, AIDON_API_KEY, AIDON_MODEL"
echo "  Ollama alternative, no Meta key: AIDON_BACKEND=ollama, AIDON_OLLAMA_ENDPOINT=http://localhost:11434,"
echo "    AIDON_MODEL=<a model pulled in Ollama>, AIDON_FAST_MODEL=<small local model>"
echo "  WhatsApp: AIDON_WHATSAPP_TOKEN, AIDON_WHATSAPP_PHONE_ID, AIDON_WHATSAPP_VERIFY_TOKEN, AIDON_WHATSAPP_APP_SECRET, AIDON_WHATSAPP_OWNER"
echo "  Slack: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, or polling with SLACK_BOT_TOKEN and a channel"
echo "  Gmail: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN"
echo "  Search/YouTube/voice/social/GitHub/Tavily: TAVILY_API_KEY, AIDON_YOUTUBE_API_KEY, AIDON_FISH_API_KEY, AIDON_THREADS_ACCESS_TOKEN, AIDON_IG_ACCESS_TOKEN, GITHUB_TOKEN"
echo "  Tunnel: put a webhook-only listener in front, never tunnel the full WebUI unauthenticated:"
echo "    ./launch.sh webui serve --webhook-only"
echo "  Steer and cancel, while a run is running, any surface, same run id:"
echo "    WebUI: POST /api/cancel {run_id}, POST /api/steer {run_id, note}, GET /api/runs"
echo "    WhatsApp/Slack: cancel <run_id>, steer <run_id> <note>"
echo "    CLI: ./launch.sh control cancel <run_id>, ./launch.sh control steer <run_id> <note>"
echo "  Demo, live testing, and Meta API key claim are after work, not in setup.sh."
