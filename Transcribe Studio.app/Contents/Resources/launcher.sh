#!/usr/bin/env bash
# Transcribe Studio — bundled launcher script.
#
# This file is loaded from inside Contents/Resources/, so the Mach-O
# launcher's read of it doesn't need TCC permission for ~/Documents.
# Once we exec into run.sh below, every process in the chain inherits
# the .app's responsible-code identifier, so the user's Full Disk
# Access grant on the .app actually applies.

set -uo pipefail

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"
URL="http://127.0.0.1:5180"

# If a server is already running, just open the browser and exit so we
# don't fight for port 5180.
if curl -sf "$URL/api/status" >/dev/null 2>&1; then
  open "$URL"
  exit 0
fi

# Background: poll for the server, then open the browser. Backgrounded
# so we can exec into run.sh below without blocking. This child inherits
# our TCC scope but doesn't actually need it — curl and open only touch
# localhost and Launch Services.
(
  for _ in $(seq 1 60); do
    if curl -sf "$URL/api/status" >/dev/null 2>&1; then
      open "$URL"
      exit 0
    fi
    sleep 0.5
  done
) &

# Mirror stdout/stderr to two places:
#   - /tmp/transcribestudio-launch.log: writable without TCC, so we can
#     diagnose denials before the Documents log dir is reachable
#   - $STUDIO/logs/app-launch.log: the usual location (requires TCC)
# We exec into run.sh so this process becomes the python server.
TMPLOG=/tmp/transcribestudio-launch.log
echo "=== launching at $(date) (pid=$$) ===" >> "$TMPLOG"
mkdir -p "$STUDIO/logs" 2>>"$TMPLOG"
exec "$STUDIO/run.sh" --no-browser >> "$STUDIO/logs/app-launch.log" 2>>"$TMPLOG"
