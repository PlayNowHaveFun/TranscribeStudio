#!/usr/bin/env bash
# Uninstall.command — stops and removes the background agent.
# Leaves the studio folder, venv, and your transcripts intact.

set -uo pipefail
cd /tmp

LABEL="com.transcribestudio"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true
if launchctl list | grep -q "$LABEL"; then
  echo "Warning: agent still visible in launchctl list."
else
  echo "Agent unloaded."
fi
rm -f "$PLIST_DST"
pkill -f "transcribe-studio/run.sh" 2>/dev/null || true
pkill -f "app.main" 2>/dev/null || true
echo "Uninstalled. Studio folder and your transcripts are untouched."
read -p "Press Enter to close..."
