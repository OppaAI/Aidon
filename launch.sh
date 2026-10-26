#!/bin/sh
# Aidon launch: start Aidon, passing arguments through to the CLI.
#
# Usage:
#   ./launch.sh goal "Plan a dinner for two"
#   ./launch.sh interview "Plan a dinner" --run
#   ./launch.sh trigger list
#   ./launch.sh plugin list
#
# What it does:
# - If the Aidon home does not exist, it runs ./setup.sh first,
#   so launch works on a fresh machine.
# - If $AIDON_HOME/.env is missing, it warns, it does not fail,
#   because Aidon can run offline tests with the FakeClient.
# - Then it runs: python3 -m src.main "$@"
#   WebUI is not built yet, so launch currently starts the
#   CLI, not a server. When WebUI is built, launch will
#   start the WebUI server instead, with the same home,
#   backend, and ingest path.
# - Docker and VM are not installed by launch.sh. Docker
#   is used only if AIDON_EXECUTION_BACKEND=docker is set
#   in .env and Docker is installed. A VM is post-hackathon,
#   future only.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AIDON_HOME=${AIDON_HOME:-$HOME/.aidon}
export AIDON_HOME

if [ ! -d "$AIDON_HOME" ]; then
  echo "Aidon home not found at $AIDON_HOME, running setup first..."
  "$REPO_DIR/setup.sh"
fi

if [ ! -f "$AIDON_HOME/.env" ]; then
  echo "WARNING: $AIDON_HOME/.env not found. Fill it in from examples/.env.example for a real model run, or run offline tests for now." >&2
fi

cd "$REPO_DIR"
exec python3 -m src.main "$@"
