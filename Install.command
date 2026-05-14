#!/usr/bin/env bash
# Install.command — full first-time setup.
# 1. Sets up venv + Flask
# 2. Installs the launchd agent (auto-start at login, runs in background)
# 3. Starts it
# 4. Opens the studio in your browser

set -uo pipefail
cd "$(dirname "$0")"

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"
PLIST_DST="$HOME/Library/LaunchAgents/com.transcribestudio.plist"
PLIST_SRC="$STUDIO/com.transcribestudio.plist"
LABEL="com.transcribestudio"

clear
cat <<EOF
══════════════════════════════════════════════════════════════════════
  Transcribe Studio — install
══════════════════════════════════════════════════════════════════════

EOF

# Step 1 — venv + dependencies
echo "[1/4] Setting up Python venv and dependencies..."
chmod +x "$STUDIO/setup.sh" "$STUDIO/run.sh" \
         "$STUDIO/Transcribe Studio.app/Contents/MacOS/TranscribeStudio" 2>/dev/null
"$STUDIO/setup.sh"

# Step 2 — quarantine cleanup (needed because files are not Finder-created)
echo ""
echo "[2/4] Removing macOS quarantine flags..."
xattr -dr com.apple.quarantine "$STUDIO" 2>/dev/null || true
echo "  done."

# Step 3 — launchd (using modern bootstrap/bootout, not deprecated load)
echo ""
echo "[3/4] Installing background agent..."
mkdir -p "$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

# Tear down anything currently loaded
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Clean up the previous Samvaad-only agent if present
OLD_LABEL="com.samvaad.transcribe"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
launchctl bootout "$DOMAIN/$OLD_LABEL" 2>/dev/null || true
launchctl unload "$OLD_PLIST" 2>/dev/null || true
if [[ -f "$OLD_PLIST" ]]; then
  rm -f "$OLD_PLIST"
  echo "  removed old com.samvaad.transcribe agent"
fi

# Write fresh plist
sed "s|REPLACE_WITH_HOME|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null

# Enable + bootstrap (modern API). enable is no-op if already enabled.
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>&1; then
  echo "  ✓ agent loaded"
else
  echo "  ✗ launchctl bootstrap failed. Trying legacy load as fallback..."
  if launchctl load "$PLIST_DST" 2>&1; then
    echo "  ✓ agent loaded (via legacy load)"
  else
    echo "  ✗ both bootstrap and load failed. Run for diagnostics:"
    echo "      sudo launchctl bootstrap $DOMAIN $PLIST_DST"
    exit 1
  fi
fi

# Verify it's actually in the list
sleep 1
if launchctl list | grep -q "$LABEL"; then
  echo "  ✓ verified in launchctl list"
else
  echo "  ⚠ agent not visible in launchctl list — backend may not start"
fi

# Step 4 — wait for backend, open browser
echo ""
echo "[4/4] Starting backend..."
URL="http://127.0.0.1:5180"
for i in $(seq 1 30); do
  sleep 0.5
  if curl -sf "$URL/api/status" >/dev/null 2>&1; then
    echo "  backend ready at $URL"
    break
  fi
done
open "$URL"

cat <<EOF

══════════════════════════════════════════════════════════════════════
  Done.

  Studio URL:   $URL
  Open anytime: double-click "Transcribe Studio.app"
                or open the URL in any browser

  IMPORTANT — Full Disk Access:
    macOS requires Full Disk Access so the app can read your videos
    and write transcripts when launched by launchd. Open:
      System Settings → Privacy & Security → Full Disk Access
    Click + and add:
      - /bin/bash
      - $STUDIO/Transcribe Studio.app
    (already granted? you can ignore this)

  NOTE — venv location:
    The Python venv lives at:
      ~/Library/Application Support/transcribe-studio/venv
    (NOT in the project folder). This avoids macOS TCC blocking
    Python at startup when launched by launchd. If you ever see
    "PermissionError ... pyvenv.cfg" in logs/launchd.err.log,
    run: ./migrate-venv.command

══════════════════════════════════════════════════════════════════════
EOF
read -p "Press Enter to close..."
