#!/bin/sh
# Aidon launch: start Aidon, WebUI by default, CLI with arguments.
#
# Usage:
#   ./launch.sh
#     Starts the WebUI server on http://127.0.0.1:8765/,
#     with WhatsApp and Slack webhooks at
#     /webhook/whatsapp and /webhook/slack.
#   ./launch.sh goal "Plan a dinner for two"
#   ./launch.sh interview "Plan a dinner" --run
#   ./launch.sh trigger list
#   ./launch.sh plugin list
#   ./launch.sh --both
#     Starts the full WebUI and a separate webhook-only listener. Slack
#     polling is enabled in the full WebUI when AIDON_SLACK_INBOUND_CHANNEL
#     is configured.
#     Passes arguments through to the CLI, instead of
#     starting the WebUI server.
#
# What it does:
# - If the Aidon home does not exist, it runs ./setup.sh first,
#   so launch works on a fresh machine.
# - If $AIDON_HOME/.env is missing, it warns, it does not fail,
#   because Aidon can run offline tests with the FakeClient.
# - With no arguments, it runs: python3 -m src.system.main webui serve
#   With arguments, it runs: python3 -m src.system.main "$@"
# - Docker and VM are not installed by launch.sh. Docker
#   is used only if AIDON_EXECUTION_BACKEND=docker is set
#   in .env and Docker is installed. A VM is post-hackathon,
#   future only.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AIDON_HOME=${AIDON_HOME:-$HOME/.aidon}
export AIDON_HOME

# Use the repo's virtualenv when it exists. Falling back to the
# system python3 silently loses qdrant-client and the embedder, so
# memory drops to the in-process store and nothing persists.
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
  PYTHON="$REPO_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "ERROR: no python3 found. Install Python 3.11 or later." >&2
  exit 1
fi

if [ ! -d "$AIDON_HOME" ]; then
  echo "Aidon home not found at $AIDON_HOME, running setup first..."
  "$REPO_DIR/setup.sh"
fi

if [ ! -f "$AIDON_HOME/.env" ] && [ ! -f "$REPO_DIR/.env" ]; then
  echo "WARNING: no .env found in $AIDON_HOME or $REPO_DIR. Fill one in from examples/.env.example for a real model run, or run offline tests for now." >&2
fi

cd "$REPO_DIR"

if [ "${1:-}" = "--both" ]; then
  # Keep the old Aidon ports so an existing Tailscale Funnel continues to
  # work: public webhook Funnel -> 8766, tailnet WebUI -> 8765.
  WEBUI_PORT=${AIDON_WEBUI_PORT:-8765}
  WEBHOOK_PORT=${AIDON_WEBHOOK_PORT:-8766}

  if command -v ss >/dev/null 2>&1; then
    for port in "$WEBUI_PORT" "$WEBHOOK_PORT"; do
      if ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q .; then
        echo "ERROR: port $port is already in use; stop that process first." >&2
        exit 1
      fi
    done
  fi

  # The public tunnel should point at this listener. It exposes only the
  # signed WhatsApp and Slack webhook endpoints.
  AIDON_SLACK_AUTOSTART=0 "$PYTHON" -m src.system.main webui serve \
    --webhook-only --port "$WEBHOOK_PORT" &
  WEBHOOK_PID=$!
  cleanup() {
    kill "$WEBHOOK_PID" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  echo "Webhook listener on :$WEBHOOK_PORT (pid $WEBHOOK_PID)"
  echo "WebUI on :$WEBUI_PORT; Ctrl+C stops both"
  AIDON_SLACK_AUTOSTART=1 "$PYTHON" -m src.system.main webui serve \
    --port "$WEBUI_PORT"
  exit $?
fi

if [ $# -eq 0 ]; then
  exec "$PYTHON" -m src.system.main webui serve \
    --port "${AIDON_WEBUI_PORT:-8765}"
else
  exec "$PYTHON" -m src.system.main "$@"
fi
