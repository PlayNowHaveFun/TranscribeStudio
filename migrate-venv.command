#!/usr/bin/env bash
# migrate-venv.command — moves the Transcribe Studio venv out of
# ~/Documents (which macOS TCC protects) and into ~/Library/Application
# Support/transcribe-studio/, where launchd-spawned Python isn't blocked.
#
# Safe to run repeatedly. Doesn't touch your projects or transcripts.
#
# What it does:
#   1. Captures `pip freeze` from the legacy venv (if any).
#   2. Creates a new venv at ~/Library/Application Support/transcribe-studio/venv.
#   3. Reinstalls everything from the freeze (demucs etc.).
#   4. Restarts the launchd agent.
#   5. Leaves the old venv in place so you can verify, then delete it manually.

set -uo pipefail
cd "$(dirname "$0")"

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"
LEGACY_VENV="$STUDIO/venv"
NEW_VENV="$HOME/Library/Application Support/transcribe-studio/venv"
LABEL="com.transcribestudio"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

echo "Transcribe Studio — migrate venv to ~/Library/Application Support"
echo ""
echo "  Old:  $LEGACY_VENV"
echo "  New:  $NEW_VENV"
echo ""

# Find a usable system Python.
PYTHON=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON="$cand"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "✗ Python 3 not found. Install with: brew install python" >&2
  read -p "Press Enter to close..."
  exit 1
fi
echo "[1/5] Using $PYTHON ($($PYTHON --version))"

# Capture legacy freeze (best-effort — venv may already be unusable).
FREEZE_FILE="$(mktemp)"
if [[ -x "$LEGACY_VENV/bin/pip" ]]; then
  echo "[2/5] Capturing legacy pip freeze..."
  "$LEGACY_VENV/bin/pip" freeze > "$FREEZE_FILE" 2>/dev/null || true
  PKG_COUNT="$(wc -l < "$FREEZE_FILE" | tr -d ' ')"
  echo "  captured $PKG_COUNT packages"
else
  echo "[2/5] No legacy venv to read — installing fresh requirements only."
fi

# Stop the agent before mutating venvs (so it doesn't keep crashing).
echo "[3/5] Stopping launchd agent..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Build the new venv.
echo "[4/5] Creating new venv..."
mkdir -p "$(dirname "$NEW_VENV")"
if [[ -d "$NEW_VENV" ]]; then
  echo "  $NEW_VENV already exists — refreshing in place"
else
  "$PYTHON" -m venv "$NEW_VENV"
fi
"$NEW_VENV/bin/pip" install --quiet --upgrade pip
echo "  installing requirements.txt"
"$NEW_VENV/bin/pip" install --quiet -r "$STUDIO/requirements.txt"

if [[ -s "$FREEZE_FILE" ]]; then
  # Strip out things requirements.txt already pinned + base stuff that always reinstalls.
  TMP_REQ="$(mktemp)"
  grep -viE '^(flask|werkzeug|jinja2|markupsafe|itsdangerous|click|blinker|pip|setuptools|wheel)([ =<>!~]|$)' \
    "$FREEZE_FILE" > "$TMP_REQ" || true
  if [[ -s "$TMP_REQ" ]]; then
    echo "  reinstalling extra packages (demucs etc.) — this can take a few minutes"
    "$NEW_VENV/bin/pip" install -r "$TMP_REQ" || \
      echo "  ⚠ some packages failed; install them manually with:"
    echo "      $NEW_VENV/bin/pip install <pkg>"
  fi
  rm -f "$TMP_REQ"
fi
rm -f "$FREEZE_FILE"

# Restart the agent.
echo "[5/5] Restarting launchd agent..."
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null; then
  echo "  agent restarted"
else
  launchctl load "$PLIST_DST" 2>/dev/null || echo "  ⚠ couldn't restart agent — run Repair.command"
fi

# Wait for backend.
URL="http://127.0.0.1:5180"
echo ""
echo "Waiting for backend at $URL..."
for i in $(seq 1 30); do
  sleep 0.5
  if curl -sf "$URL/api/status" >/dev/null 2>&1; then
    echo "✓ backend ready"
    break
  fi
done

echo ""
echo "Done. The legacy venv at:"
echo "  $LEGACY_VENV"
echo "is left in place so you can verify. Once everything works, delete it:"
echo "  rm -rf \"$LEGACY_VENV\""
echo ""
read -p "Press Enter to close..."
