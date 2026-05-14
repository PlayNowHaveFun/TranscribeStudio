#!/usr/bin/env bash
# Open Studio.command — opens the studio in your default browser.
# If the backend isn't running, attempts to start it via the .app launcher.

set -uo pipefail
cd /tmp

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"
URL="http://127.0.0.1:5180"

if ! curl -sf "$URL/api/status" >/dev/null 2>&1; then
  echo "Backend not running. Starting it..."
  nohup "$STUDIO/run.sh" --no-browser > "$STUDIO/logs/app-launch.log" 2>&1 &
  for _ in $(seq 1 30); do
    sleep 0.5
    if curl -sf "$URL/api/status" >/dev/null 2>&1; then break; fi
  done
fi

open "$URL"
