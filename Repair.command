#!/usr/bin/env bash
# Repair.command — re-bootstrap the launchd agent if Install.command had issues.
# Safe to run multiple times. Doesn't touch your venv, projects, or transcripts.

set -uo pipefail

LABEL="com.transcribestudio"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PLIST_SRC="$HOME/Documents/cowork-tools/transcribe-studio/com.transcribestudio.plist"
DOMAIN="gui/$(id -u)"

echo "Repairing Transcribe Studio agent..."

# Tear down whatever's there
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Re-write the plist with the right home dir
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|REPLACE_WITH_HOME|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null

# Bootstrap
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
if launchctl bootstrap "$DOMAIN" "$PLIST_DST"; then
  echo "✓ bootstrap succeeded"
else
  echo "✗ bootstrap failed. Trying legacy load..."
  launchctl load "$PLIST_DST" || { echo "✗ load also failed"; exit 1; }
fi

# Wait for the backend
URL="http://127.0.0.1:5180"
echo "Waiting for backend at $URL..."
for i in $(seq 1 30); do
  sleep 0.5
  if curl -sf "$URL/api/status" >/dev/null 2>&1; then
    echo "✓ backend ready"
    open "$URL"
    break
  fi
done

# Final status
echo ""
launchctl list | grep "$LABEL" || echo "⚠ not visible in launchctl list"
echo ""
echo "If the studio still won't open, check:"
echo "  - $HOME/Documents/cowork-tools/transcribe-studio/logs/launchd.err.log"
echo "  - System Settings → Privacy & Security → Full Disk Access"
echo "    (add /bin/bash and Transcribe Studio.app)"
read -p "Press Enter to close..."
