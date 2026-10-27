#!/bin/sh
# Aidon launch: start Aidon, WebUI by default, CLI with arguments.
#
# Usage:
#   ./launch.sh
#     Starts the WebUI server on http://127.0.0.1:8002/,
#     with WhatsApp and Slack webhooks at
#     /webhook/whatsapp and /webhook/slack.
#   ./launch.sh goal "Plan a dinner for two"
#   ./launch.sh interview "Plan a dinner" --run
#   ./launch.sh trigger list
#   ./launch.sh plugin list
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
if [ $# -eq 0 ]; then
  exec "$PYTHON" -m src.system.main webui serve
else
  exec "$PYTHON" -m src.system.main "$@"
fi
