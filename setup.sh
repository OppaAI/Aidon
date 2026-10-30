#!/bin/sh
# Aidon setup: first-time setup, idempotent, run it twice safely.
#
# Interactive by default, like the Aidon setup: it asks for
# things one by one, and every answer is skippable.
#   - A model backend first: Meta Model API, a native chat endpoint,
#     or another OpenAI-compatible endpoint.
#   - Then model names, one by one: Enter keeps the shown default,
#     typing replaces it.
#   - Then API keys, one by one: a key already set is never asked
#     again, an empty answer skips that feature (it stays off, and
#     can be added later in .env or through the WebUI).
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
# Run the Qdrant vector-memory server in Docker
# without asking:
#   ./setup.sh --with-qdrant
# Run SearXNG web search in Docker without asking:
#   ./setup.sh --with-searxng
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
# Usage: ./setup.sh [--non-interactive] [--with-optional] [--with-qdrant] [--with-searxng]
set -eu

# Meta-inspired terminal palette: deep blue, cyan, violet, green, amber.
# Colors turn off automatically when output is redirected or --non-interactive
# is used, so logs remain clean and scripts stay pipe-friendly.
if [ -t 1 ] 2>/dev/null; then
  RESET=$(printf '\033[0m'); BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
  BLUE=$(printf '\033[38;5;33m'); CYAN=$(printf '\033[38;5;51m')
  VIOLET=$(printf '\033[38;5;141m'); GREEN=$(printf '\033[38;5;78m')
  AMBER=$(printf '\033[38;5;220m'); RED=$(printf '\033[38;5;203m')
else
  RESET=''; BOLD=''; DIM=''; BLUE=''; CYAN=''; VIOLET=''; GREEN=''; AMBER=''; RED=''
fi
say() { printf '%s\n' "$*"; }
title() { printf '%s\n' "${BLUE}${BOLD}$*${RESET}"; }
section() { printf '\n%s\n' "${VIOLET}${BOLD}◆ $*${RESET}"; }
info() { printf '%s %s\n' "${CYAN}·${RESET}" "$*"; }
ok() { printf '%s %s\n' "${GREEN}✓${RESET}" "$*"; }
warn() { printf '%s %s\n' "${AMBER}!${RESET}" "$*" >&2; }
fail() { printf '%s %s\n' "${RED}✗${RESET}" "$*" >&2; }

title "  Aidon  ·  setup"
info "Preparing a calm, local workspace for your agent."

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AIDON_HOME=${AIDON_HOME:-$HOME/.aidon}
NONINTERACTIVE=0
WITH_OPTIONAL=0
WITH_QDRANT=0
WITH_SEARXNG=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive) NONINTERACTIVE=1 ;;
    --with-optional) WITH_OPTIONAL=1 ;;
    --with-qdrant) WITH_QDRANT=1 ;;
    --with-searxng) WITH_SEARXNG=1 ;;
    -h|--help)
      say "usage: ./setup.sh [--non-interactive] [--with-optional] [--with-qdrant] [--with-searxng]"
      exit 0 ;;
    *) fail "I don't recognize '$arg'. Try --help."; exit 1 ;;
  esac
done

info "Workspace: $AIDON_HOME"
mkdir -p "$AIDON_HOME/workspace" "$AIDON_HOME/ingest" "$AIDON_HOME/plugins" "$AIDON_HOME/skills" "$AIDON_HOME/specs" "$AIDON_HOME/library/files" "$AIDON_HOME/runs" "$AIDON_HOME/summaries" "$AIDON_HOME/learned" "$AIDON_HOME/email/drafts" "$AIDON_HOME/trace" "$AIDON_HOME/v1" "$AIDON_HOME/live" "$AIDON_HOME/dags"

if ! command -v python3 >/dev/null 2>&1; then
  fail "Python 3.11+ is required. Install it, then run ./setup.sh again."
  exit 1
fi
ok "Python ready: $(python3 --version 2>&1)"

# Install the project itself and its lightweight base dependencies. Heavy
# ML and optional capability packages are offered below, one by one. The old
# Aidon setup did this through uv; without this step a fresh machine falls
# back to system Python and silently misses the installed package/runtime.
if command -v uv >/dev/null 2>&1; then
  section "Installing the core"
  info "Syncing the declared dependencies with uv."
  (cd "$REPO_DIR" && uv sync) || {
    warn "uv sync failed; optional features may be unavailable."
  }
else
  warn "uv is not installed; using the available Python environment."
  info "Install uv and rerun setup.sh for the declared dependencies."
fi

# All dependency checks and fallback installs must use the same interpreter
# launch.sh will use.  Checking system python3 here can report a package as
# missing even when it is already installed in the project's .venv.
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
  PYTHON="$REPO_DIR/.venv/bin/python"
else
  PYTHON=python3
fi
info "Dependency environment: $PYTHON"

if [ -f "$AIDON_HOME/.env" ]; then
  ok "Keeping your existing environment file."
else
  if [ -f "$REPO_DIR/examples/.env.example" ]; then
    cp "$REPO_DIR/examples/.env.example" "$AIDON_HOME/.env"
    ok "Created a starting environment file from examples/.env.example."
  else
    warn "examples/.env.example was not found; no .env was created."
  fi
fi
# Secrets file, owner-only, whether it was just created or already there.
chmod 600 "$AIDON_HOME/.env" 2>/dev/null || true

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
  if [ -n "$cur" ]; then ok "$1 is already set."; return 0; fi
  val=""
  printf '%s%s%s %s%s%s ' "$CYAN" "$2" "$RESET" "$DIM" "(leave blank to skip):" "$RESET" >&2
  read -r val || true
  [ -z "$val" ] && return 0
  dotenv_set "$1" "$val"
}
ask_setting() {
  if [ "$NONINTERACTIVE" = 1 ]; then return 0; fi
  cur=$(dotenv_get "$1")
  if [ -z "$cur" ]; then cur="${3:-}"; fi
  val=""
  printf '%s%s%s %s[%s]%s: ' "$CYAN" "$2" "$RESET" "$DIM" "$cur" "$RESET" >&2
  read -r val || true
  [ -z "$val" ] && val="$cur"
  dotenv_set "$1" "$val"
}
ask_yes_no() {
  if [ "$NONINTERACTIVE" = 1 ]; then return 1; fi
  yn=""
  printf '%s%s%s %s[y/N]%s: ' "$CYAN" "$1" "$RESET" "$DIM" "$RESET" >&2
  read -r yn || true
  case "${yn:-n}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}
install_package() {
  # uv add first, as requested: the package becomes a project
  # dependency (in the matching optional group, or the main list
  # for qdrant-client), pyproject.toml and uv.lock are updated,
  # and the package is installed into the project environment.
  # That means setup.sh now modifies pyproject.toml and uv.lock:
  # commit them if you want other machines to get the same deps.
  # Fallbacks, in order, when uv add cannot run: uv pip install
  # (installs into the venv without touching pyproject), then
  # plain pip install. A failed install is never fatal here:
  # the feature simply stays off until the package is installed.
  pkg="$1"
  extra="$2"
  if command -v uv >/dev/null 2>&1 && [ -f "$REPO_DIR/pyproject.toml" ]; then
    if [ -n "$extra" ]; then
      (cd "$REPO_DIR" && uv add --optional "$extra" "$pkg") && return 0
    else
      (cd "$REPO_DIR" && uv add "$pkg") && return 0
    fi
    warn "uv add of $pkg failed; trying the project environment instead."
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON" "$pkg" && return 0
    warn "uv pip install of $pkg failed; trying pip instead."
  fi
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m pip install "$pkg" || warn "Could not install $pkg; its feature will stay off."
  else
    warn "No pip or uv found. Install $pkg yourself, then run ./setup.sh again."
  fi
}

install_qdrant_docker() {
  # Qdrant vector-memory server, Docker. Embedded local mode allows
  # only ONE Aidon process at a time; the WebUI + tick loop +
  # listeners need a server. Pinned image for reproducible installs.
  # Reuses a container named aidon-qdrant when it already exists
  # (starts it if stopped); otherwise creates it with data in
  # $AIDON_HOME/qdrant-server. Note: the retired binary install
  # kept its data one level down, in $datadir/storage — if you
  # migrate with data, move that directory's contents up one
  # level into $datadir first.
  # Returns 0 when the server is running, 1 when it cannot be.
  qver="v1.19.1"
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker is not installed; install it, then run ./setup.sh again."
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    warn "Docker is not running or not accessible; start it, then run ./setup.sh again."
    return 1
  fi
  # Retire the old binary + systemd install when present: it would
  # bind the same port and fight the container.
  if [ -f "$HOME/.config/systemd/user/aidon-qdrant.service" ]; then
    systemctl --user stop aidon-qdrant.service >/dev/null 2>&1 || true
    systemctl --user disable aidon-qdrant.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/aidon-qdrant.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    info "Retired the old aidon-qdrant systemd service."
  fi
  datadir="$AIDON_HOME/qdrant-server"
  mkdir -p "$datadir"
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'aidon-qdrant'; then
    info "Qdrant container aidon-qdrant already exists — reusing it."
    docker start aidon-qdrant >/dev/null 2>&1 || true
  else
    info "Starting Qdrant $qver in Docker (aidon-qdrant)."
    docker run -d --name aidon-qdrant --restart unless-stopped \
      -p 127.0.0.1:6333:6333 \
      -v "$datadir:/qdrant/storage" \
      "qdrant/qdrant:$qver" >/dev/null || return 1
  fi
  i=0
  healthy=0
  while [ "$i" -lt 20 ]; do
    if curl -sf http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  if [ "$healthy" = 1 ]; then
    ok "Qdrant healthy: http://127.0.0.1:6333."
  else
    warn "Qdrant not healthy after 20s — check its log (docker logs aidon-qdrant)."
  fi
  if [ -z "$(dotenv_get AIDON_QDRANT_URL)" ]; then
    dotenv_set AIDON_QDRANT_URL "http://127.0.0.1:6333"
    ok "Set AIDON_QDRANT_URL=http://127.0.0.1:6333 in .env."
  else
    info "AIDON_QDRANT_URL already set — leaving it."
  fi
  return 0
}

install_searxng_docker() {
  # SearXNG web-search server, Docker, for the searxng search
  # backend (AIDON_SEARXNG_URL, default http://localhost:8888).
  # Reuses a container named aidon-searxng when it already exists;
  # otherwise creates it. Settings live in $AIDON_HOME/searxng and
  # are written only once, so later setup runs never overwrite
  # your tweaks. Returns 0 when the server is running, 1 when it
  # cannot be.
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker is not installed; install it, then run ./setup.sh again."
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    warn "Docker is not running or not accessible; start it, then run ./setup.sh again."
    return 1
  fi
  confdir="$AIDON_HOME/searxng"
  mkdir -p "$confdir"
  if [ ! -f "$confdir/settings.yml" ]; then
    # JSON format for Aidon's /search?format=json calls, and the
    # limiter off: a local agent's query rate trips it otherwise.
    # Other settings stay at the image defaults (merged, not replaced).
    cat > "$confdir/settings.yml" <<EOF
general:
  instance_name: "aidon-searxng"
search:
  formats:
    - html
    - json
server:
  limiter: false
EOF
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'aidon-searxng'; then
    info "SearXNG container aidon-searxng already exists — reusing it."
    docker start aidon-searxng >/dev/null 2>&1 || true
  else
    info "Starting SearXNG in Docker (aidon-searxng)."
    secret=$(openssl rand -hex 32 2>/dev/null || echo aidon-searxng-local)
    docker run -d --name aidon-searxng --restart unless-stopped \
      -p 127.0.0.1:8888:8080 \
      -e "SEARXNG_SECRET=$secret" \
      -v "$confdir:/etc/searxng:rw" \
      searxng/searxng >/dev/null || return 1
  fi
  i=0
  healthy=0
  while [ "$i" -lt 20 ]; do
    if curl -sf http://127.0.0.1:8888/healthz >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  if [ "$healthy" = 1 ]; then
    ok "SearXNG healthy: http://127.0.0.1:8888."
  else
    warn "SearXNG not healthy after 20s — check its log (docker logs aidon-searxng)."
  fi
  return 0
}

if [ "$NONINTERACTIVE" = 0 ]; then
  section "Choosing your model"
  info "Aidon needs a model backend. Choose the one you want to use:"
  info "1) Meta Model API (requires a key)"
  info "2) Native chat endpoint (local or hosted)"
  info "3) Another compatible endpoint"
  cur_backend=$(dotenv_get "AIDON_BACKEND")
  cur_endpoint=$(dotenv_get "AIDON_API_ENDPOINT")
  default_choice=1
  if [ "$cur_backend" = "native" ]; then default_choice=2; fi
  case "$cur_endpoint" in
    *localhost:11434*|*127.0.0.1:11434*) default_choice=2 ;;
  esac
  choice=""
  printf '%sChoose a backend [1/2/3, default %s]%s: ' "$CYAN" "$default_choice" "$RESET" >&2
  read -r choice || true
  [ -z "$choice" ] && choice="$default_choice"
  case "$choice" in
    2)
      dotenv_set "AIDON_BACKEND" "native"
      ok "Native endpoint selected; Aidon will not ask for an API key."
      # A Meta model name left over from .env.example is not a native
      # model; clear it so Enter does not keep it by accident.
      case "$(dotenv_get "AIDON_MODEL")" in
        muse-spark*) dotenv_set "AIDON_MODEL" "" ;;
      esac
      ask_setting "AIDON_API_ENDPOINT" "Native model endpoint" ""
      ask_setting "AIDON_MODEL" "Model name"
      ask_setting "AIDON_FAST_MODEL" "Fast-tier model (empty = no routing)"
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
        warn "Meta backend selected without an API key. Add AIDON_API_KEY before running a real goal."
      fi
      ;;
  esac
  section "Memory and research"
  info "Changing the embedding model later requires a fresh memory store."
  ask_setting "AIDON_EMBED_MODEL" "Embedding model" "google/embeddinggemma-2"
  ask_setting "AIDON_RESEARCH_ALLOWLIST" "Research allowlist, comma domains (empty = unrestricted)"
  section "Optional connections"
  info "Each key enables one feature. Leave it blank to configure it later."
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
  section "Accounts and delivery"
  info "Add the account IDs and destinations connected to the keys above."
  info "This is where reports, approvals, and incoming messages can reach you."
  ask_key "AIDON_THREADS_ACCOUNT_ID" "Threads account ID (for publishing)"
  ask_key "AIDON_IG_USER_ID" "Instagram user ID (for publishing)"
  ask_key "AIDON_OWNER_EMAIL" "Owner email (you, for drafts and reports to you)"
  ask_setting "AIDON_SLACK_INBOUND_CHANNEL" "Slack inbound channel ID (empty = webhooks only, no polling)"
  ask_setting "AIDON_SLACK_POLL_SECONDS" "Slack poll interval in seconds" "30"
  ask_setting "AIDON_APPROVAL_NUDGE_CHANNEL" "Slack channel for approval nudges (empty = no nudges)"
  ask_setting "AIDON_DAILY_REPORT_TIME" "Daily report time, HH:MM (empty = no daily report)"
  ask_setting "AIDON_TIMEZONE" "Timezone" "America/Vancouver"
  ask_key "AIDON_TTS_VOICE_ID" "Fish Audio TTS voice ID (empty = default voice)"
  ask_setting "AIDON_SEARCH_BACKEND" "Search backend, tavily or searxng" "tavily"
  ask_setting "AIDON_SEARXNG_URL" "SearXNG URL (only used when backend is searxng)" "http://localhost:8888"
  if [ "$(dotenv_get AIDON_SEARCH_BACKEND)" = "searxng" ]; then
    if [ "$WITH_SEARXNG" = 1 ]; then
      install_searxng_docker || warn "SearXNG Docker install failed; continuing without it."
    elif ask_yes_no "Run SearXNG in Docker (aidon-searxng on localhost:8888)?"; then
      install_searxng_docker || warn "SearXNG Docker install failed; continuing without it."
    else
      info "Skipped; SearXNG stays off until a server URL is set."
    fi
  fi
  section "Qdrant vector memory"
  info "Local mode works for one process; a server shares memory across WebUI, tick loop, and listeners."
  QDRANT_URL_DEFAULT=""
  if [ "$WITH_QDRANT" = 1 ]; then
    if install_qdrant_docker; then QDRANT_URL_DEFAULT="http://127.0.0.1:6333"; fi
  elif ask_yes_no "Run Qdrant in Docker (aidon-qdrant on localhost:6333)?"; then
    if install_qdrant_docker; then QDRANT_URL_DEFAULT="http://127.0.0.1:6333"; fi
  else
    info "Skipped; memory stays on local mode until a Qdrant server URL is set."
  fi
  ask_setting "AIDON_QDRANT_URL" "Qdrant server URL (empty = local mode under the Aidon home)" "$QDRANT_URL_DEFAULT"
  ask_setting "AIDON_EXECUTION_BACKEND" "Execution backend, bubblewrap or docker" "bubblewrap"
  ask_setting "AIDON_IMAGE_MODEL" "Image model" "muse-image-1.0"
  ask_setting "AIDON_SAM_MODEL" "SAM model" "meta-sam"
  ask_key "AIDON_MCP_TOKEN" "MCP server token (required when another app calls Aidon's MCP server)"
  ask_key "AIDON_HUB_TOKEN" "Remote hub token (for hub serve / hub push)"
  ask_setting "AIDON_MCP_URL" "MCP HTTP endpoint to call (empty = no external MCP server)"
  section "One last note"
  info "Gmail sends from the account that granted the refresh token."
  info "Keep secrets in .env only; setup never prints them back."
fi

# --with-qdrant on a non-interactive run: the interactive block above is
# skipped, so install here instead (interactive runs already handled it).
if [ "$WITH_QDRANT" = 1 ] && [ "$NONINTERACTIVE" = 1 ]; then
  section "Qdrant vector memory"
  install_qdrant_docker || warn "Qdrant Docker install failed; continuing without it."
fi

# --with-searxng on a non-interactive run: same shape as Qdrant above.
if [ "$WITH_SEARXNG" = 1 ] && [ "$NONINTERACTIVE" = 1 ]; then
  section "SearXNG web search"
  install_searxng_docker || warn "SearXNG Docker install failed; continuing without it."
fi

if command -v bwrap >/dev/null 2>&1; then
  ok "Sandbox ready: bubblewrap is available."
else
  warn "bubblewrap is not installed; the fallback sandbox will run in the workdir."
  if [ "$NONINTERACTIVE" = 0 ] && command -v apt-get >/dev/null 2>&1; then
    if ask_yes_no "Install bubblewrap now via apt (needs sudo)?"; then
      sudo apt-get install -y bubblewrap || warn "bubblewrap installation failed; install it yourself when convenient."
    fi
  fi
fi

if command -v docker >/dev/null 2>&1; then
  ok "Docker is available: $(docker --version 2>&1 | head -n 1)."
  info "Set AIDON_EXECUTION_BACKEND=docker in .env when you want to use it."
else
  info "Docker is not installed; it is only needed for the Docker backend."
  info "macOS: install Docker Desktop, or use brew install --cask docker."
  info "Ubuntu: install docker.io, then add your user to the docker group."
  info "Until then, Aidon will use the bubblewrap backend when available."
fi

info "No virtual machine is installed; setup keeps the host configuration unchanged."

section "Optional capabilities"
info "Each package enables one feature. Skip anything you do not need today."
for spec in \
  "qdrant-client:qdrant_client:Qdrant vector memory:memory" \
  "sentence-transformers:sentence_transformers:real semantic embeddings:embed" \
  "fastembed:fastembed:learned SPLADE sparse recall:sparse" \
  "Pillow:PIL:image handling and video annotation:vision" \
  "pypdf:pypdf:PDF reading:kb" \
  "python-docx:docx:Word reading:kb" \
  "beautifulsoup4:bs4:HTML reading:kb" \
  "ebooklib:ebooklib:EPUB building:docs" \
  "markdown:markdown:EPUB building:docs" \
  "markitdown:markitdown:universal document reading:docs" \
  "openpyxl:openpyxl:spreadsheet output:office" \
  "python-pptx:pptx:presentation output:office" \
  "pytesseract:pytesseract:OCR support:office" \
  ; do
  pkg=$(printf '%s' "$spec" | cut -d: -f1)
  mod=$(printf '%s' "$spec" | cut -d: -f2)
  what=$(printf '%s' "$spec" | cut -d: -f3)
  extra=$(printf '%s' "$spec" | cut -d: -f4)
  if "$PYTHON" -c "import $mod" 2>/dev/null; then
    if [ "$pkg" = "pytesseract" ] && ! command -v tesseract >/dev/null 2>&1; then
      warn "$pkg is installed, but the tesseract binary is not on PATH; install tesseract-ocr to enable OCR."
    else
      ok "$pkg is ready — $what."
    fi
  else
    warn "$pkg is not installed — $what."
    if [ "$WITH_OPTIONAL" = 1 ]; then
      info "Installing $pkg because --with-optional was requested."
      install_package "$pkg" "$extra"
    elif [ "$NONINTERACTIVE" = 0 ] && ask_yes_no "Install $pkg ($what)?"; then
      install_package "$pkg" "$extra"
    else
      info "Skipped; $what will stay off until $pkg is installed."
    fi
  fi
done

section "You are ready"
ok "Setup is complete. Your environment is at $AIDON_HOME/.env."
info "Review anything you skipped, then start Aidon with ./launch.sh."
info "For help configuring messages, approvals, or the WebUI, use the settings page."
