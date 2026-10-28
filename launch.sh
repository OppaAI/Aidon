#!/bin/sh
# Aidon launch: start Aidon, WebUI by default, CLI with arguments.
#
# Usage:
#   ./launch.sh
#     Starts the WebUI server on http://127.0.0.1:8765/,
#     with WhatsApp and Slack webhooks at
#     /webhook/whatsapp and /webhook/slack.
#   ./launch.sh goal "Plan a dinner for two"
#   ./launch.sh --cli
#     Opens the conversational terminal interface.
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

if [ -t 1 ] 2>/dev/null; then
  RESET=$(printf '\033[0m'); BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
  BLUE=$(printf '\033[38;5;33m'); CYAN=$(printf '\033[38;5;51m')
  VIOLET=$(printf '\033[38;5;141m'); GREEN=$(printf '\033[38;5;78m')
  AMBER=$(printf '\033[38;5;220m'); RED=$(printf '\033[38;5;203m')
else
  RESET=''; BOLD=''; DIM=''; BLUE=''; CYAN=''; VIOLET=''; GREEN=''; AMBER=''; RED=''
fi
title() { printf '%s\n' "${BLUE}${BOLD}$*${RESET}"; }
section() { printf '\n%s\n' "${VIOLET}${BOLD}◆ $*${RESET}"; }
info() { printf '%s %s\n' "${CYAN}·${RESET}" "$*"; }
ok() { printf '%s %s\n' "${GREEN}✓${RESET}" "$*"; }
warn() { printf '%s %s\n' "${AMBER}!${RESET}" "$*" >&2; }
fail() { printf '%s %s\n' "${RED}✗${RESET}" "$*" >&2; }

title "  Aidon  ·  launch"

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
  fail "Python 3.11 or later is required. Install it, then run ./setup.sh."
  exit 1
fi

if [ ! -d "$AIDON_HOME" ]; then
  section "First launch"
  info "I could not find $AIDON_HOME, so I will prepare it now."
  "$REPO_DIR/setup.sh"
fi

if [ ! -f "$AIDON_HOME/.env" ] && [ ! -f "$REPO_DIR/.env" ]; then
  warn "No .env found. Run ./setup.sh or copy examples/.env.example before a real model run."
fi

cd "$REPO_DIR"

if [ "${1:-}" = "--both" ]; then
  section "Starting WebUI and webhook listener"
  # Keep the Aidon ports so an existing Tailscale Funnel continues to
  # work: public webhook Funnel -> 8766, tailnet WebUI -> 8765.
  WEBUI_PORT=${AIDON_WEBUI_PORT:-8765}
  WEBHOOK_PORT=${AIDON_WEBHOOK_PORT:-8766}

  if command -v ss >/dev/null 2>&1; then
    for port in "$WEBUI_PORT" "$WEBHOOK_PORT"; do
      if ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q .; then
        fail "Port $port is already in use. Stop that process, then try again."
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

  ok "Webhook listener is running on :$WEBHOOK_PORT (pid $WEBHOOK_PID)."
  info "WebUI is running on :$WEBUI_PORT. Press Ctrl+C to stop both."
  AIDON_SLACK_AUTOSTART=1 "$PYTHON" -m src.system.main webui serve \
    --port "$WEBUI_PORT"
  exit $?
fi

if [ $# -eq 0 ]; then
  info "Opening the WebUI at http://127.0.0.1:${AIDON_WEBUI_PORT:-8765}/"
  exec "$PYTHON" -m src.system.main webui serve \
    --port "${AIDON_WEBUI_PORT:-8765}"
else
  info "Running your Aidon command."
  exec "$PYTHON" -m src.system.main "$@"
fi
