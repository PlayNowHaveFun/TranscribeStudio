#!/usr/bin/env bash
# setup.sh — one-time setup for Transcribe Studio.
# Creates a venv, installs Flask, validates whisper-cli + ffmpeg.
#
# Venv lives at ~/Library/Application Support/transcribe-studio/venv (not
# in the project root). macOS TCC protects ~/Documents at the kernel
# level, and even with Full Disk Access granted to /bin/bash, the venv's
# python3 binary (Python.framework) gets EPERM reading pyvenv.cfg under
# ~/Documents when launched by launchd. Putting the venv under
# ~/Library/Application Support sidesteps this entirely.

set -euo pipefail

cd "$(dirname "$0")"

echo "→ Looking for Python 3..."
PYTHON=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON="$cand"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "✗ Python 3 not found. Install with: brew install python" >&2
  exit 1
fi
echo "  using $PYTHON ($($PYTHON --version))"

# Venv lives outside ~/Documents to avoid macOS TCC blocking Python at startup.
VENV_DIR="${TS_VENV_DIR:-$HOME/Library/Application Support/transcribe-studio/venv}"
LEGACY_VENV="$(pwd)/venv"

mkdir -p "$(dirname "$VENV_DIR")"

if [[ ! -d "$VENV_DIR" ]]; then
  if [[ -d "$LEGACY_VENV" ]]; then
    echo "→ Legacy venv detected at $LEGACY_VENV."
    echo "  Creating new venv at:"
    echo "    $VENV_DIR"
    echo "  (the old one will be left in place; remove it manually once you're"
    echo "   sure everything works)."
  else
    echo "→ Creating venv at:"
    echo "    $VENV_DIR"
  fi
  "$PYTHON" -m venv "$VENV_DIR"
fi

echo "→ Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt
echo "  Flask installed."

# If the legacy venv had demucs (or other heavy deps), reinstall them in the
# new venv by replaying its pip freeze. Skip if user supplied TS_VENV_DIR.
if [[ -z "${TS_VENV_DIR:-}" && -x "$LEGACY_VENV/bin/pip" && "$VENV_DIR" != "$LEGACY_VENV" ]]; then
  if "$LEGACY_VENV/bin/pip" show demucs >/dev/null 2>&1 \
     && ! "$VENV_DIR/bin/pip" show demucs >/dev/null 2>&1; then
    echo "→ Migrating heavy deps (demucs etc.) from legacy venv..."
    TMP_REQ="$(mktemp)"
    "$LEGACY_VENV/bin/pip" freeze \
      | grep -viE '^(flask|werkzeug|jinja2|markupsafe|itsdangerous|click|blinker|pip|setuptools|wheel)([ =<>!~]|$)' \
      > "$TMP_REQ" || true
    if [[ -s "$TMP_REQ" ]]; then
      "$VENV_DIR/bin/pip" install --quiet -r "$TMP_REQ" || \
        echo "  ⚠ some packages failed to reinstall. Run migrate-venv.command for details."
    fi
    rm -f "$TMP_REQ"
  fi
fi

echo ""
echo "→ Checking whisper-cli + ffmpeg..."
MISSING=0
if ! command -v whisper-cli >/dev/null 2>&1 && ! command -v whisper-cpp >/dev/null 2>&1; then
  echo "  ✗ whisper-cli not found.  Install: brew install whisper-cpp"
  MISSING=1
else
  echo "  ✓ whisper-cli found"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "  ✗ ffmpeg not found.  Install: brew install ffmpeg"
  MISSING=1
else
  echo "  ✓ ffmpeg found"
fi

MODELS_DIR="$HOME/Documents/cowork-tools/whisper-models"
if [[ ! -f "$MODELS_DIR/ggml-large-v3.bin" ]]; then
  echo "  ⚠ ggml-large-v3.bin not in $MODELS_DIR"
  echo "    Download (3 GB):"
  echo "      mkdir -p \"$MODELS_DIR\""
  echo "      curl -L --fail -o \"$MODELS_DIR/ggml-large-v3.bin\" \\"
  echo "        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin"
  MISSING=1
else
  echo "  ✓ large-v3 model present"
fi
if [[ ! -f "$MODELS_DIR/ggml-silero-v6.2.0.bin" ]]; then
  echo "  ⚠ Silero VAD model not found"
  echo "    curl -L --fail -o \"$MODELS_DIR/ggml-silero-v6.2.0.bin\" \\"
  echo "      https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin"
else
  echo "  ✓ Silero VAD model present"
fi

if [[ $MISSING -eq 1 ]]; then
  echo ""
  echo "→ Some prerequisites are missing (see ⚠ above). Studio still installs OK,"
  echo "  but transcription jobs will fail until they're resolved."
fi

echo ""
echo "→ Setup complete."
