#!/usr/bin/env bash
# Create a private X11 cookie usable inside the container, then start Aidon.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DISPLAY:?Run this script from your X11 desktop session}"
command -v xauth >/dev/null || { printf '%s\n' 'xauth is required on the desktop host.' >&2; exit 1; }
source_authority="${XAUTHORITY:-${HOME}/.Xauthority}"
if [ ! -r "$source_authority" ]; then
  printf '%s\n' 'Cannot read the X11 authorization file for this desktop session.' >&2
  exit 1
fi
umask 077
desktop_auth_dir=$(mktemp -d "${TMPDIR:-/tmp}/aidon-desktop-XXXXXXXX")
trap 'rm -rf "$desktop_auth_dir"' ERR
export AIDON_DESKTOP_XAUTHORITY="$desktop_auth_dir/Xauthority"
touch "$AIDON_DESKTOP_XAUTHORITY"
cp "$source_authority" "$desktop_auth_dir/source-authority"
xauth -f "$desktop_auth_dir/source-authority" nlist "$DISPLAY" | sed 's/^..../ffff/' | \
  xauth -f "$AIDON_DESKTOP_XAUTHORITY" nmerge -
rm -f "$desktop_auth_dir/source-authority"
if [ ! -s "$AIDON_DESKTOP_XAUTHORITY" ]; then
  printf '%s\n' 'No X11 authorization was found for DISPLAY.' >&2
  rm -rf "$desktop_auth_dir"
  exit 1
fi
docker compose -f compose.yaml -f compose.desktop.yaml up -d --build aidon aidon-webhooks
trap - ERR
printf '%s\n' 'Desktop access enabled for this Aidon container. Restart it with ordinary Compose to disable desktop access.'
