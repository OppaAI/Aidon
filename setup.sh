#!/bin/sh
# Aidon setup: first-time setup, idempotent, run it twice safely.
#
# What it does:
# - Checks python3, reports the version, does not install python.
# - Creates the Aidon home (AIDON_HOME, or ~/.aidon by default)
#   with workspace, ingest, plugins, skills, specs, and
#   library/files directories. The ingest folder is the
#   user's own path, see README Triggers section.
# - Copies examples/.env.example to $AIDON_HOME/.env only
#   if $AIDON_HOME/.env does not already exist, never
#   overwriting a file you already filled in.
# - Checks bubblewrap and Docker, reports what is available,
#   and prints install instructions if Docker is missing.
#   It does NOT install Docker, and it does NOT install a
#   VM. Docker is installed by you, on the machine where
#   Aidon runs, if you want the Docker backend. A VM is
#   post-hackathon, future only, not installed here.
# - Does NOT install heavy Python packages by default.
#   Aidon runs on the standard library. Optional packages
#   (qdrant-client, sentence-transformers, fastembed) are
#   reported as missing or present, not installed, unless
#   you run setup with --with-optional, which installs
#   them with pip into the current Python environment.
#
# Usage: ./setup.sh [--with-optional]
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AIDON_HOME=${AIDON_HOME:-$HOME/.aidon}

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
    echo "Created $AIDON_HOME/.env from examples/.env.example. Fill in your endpoint, key, and model name there."
  else
    echo "WARNING: examples/.env.example not found, no .env created." >&2
  fi
fi

if command -v bwrap >/dev/null 2>&1; then
  echo "bubblewrap: available, sandbox backend is isolated."
else
  echo "bubblewrap: not installed, sandbox fallback runs in the workdir and is labeled not isolated."
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

for pkg in qdrant_client sentence_transformers fastembed; do
  if python3 -c "import $pkg" 2>/dev/null; then
    echo "optional package $pkg: present"
  else
    echo "optional package $pkg: missing (Aidon still runs without it, with HashEmbedder fallback)"
  fi
done

if [ "${1:-}" = "--with-optional" ]; then
  echo "Installing optional packages with pip..."
  python3 -m pip install qdrant-client sentence-transformers fastembed
fi

echo "Setup done. Next: fill in $AIDON_HOME/.env, then ./launch.sh"
echo ""
echo "Keys and setup, by you, after this script, names only, no values here:"
echo "  Meta Model API: AIDON_API_ENDPOINT, AIDON_API_KEY, AIDON_MODEL"
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
echo "  Adaptive DAG benchmark, canned, not live: ./launch.sh benchmark learn"
echo "  Demo, live testing, and Meta API key claim are after work, not in setup.sh."
echo ""
echo "Local models, no Ollama-specific code needed, generic endpoint plus model name:"
echo "  AIDON_API_ENDPOINT=http://localhost:11434/v1  AIDON_MODEL=<strong local model>"
echo "  AIDON_FAST_MODEL=<small local model>  for the fast tier, interpret/learn/respond phases"
echo "  A local model is a model name on a local endpoint, not a separate implementation."
