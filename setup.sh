#!/usr/bin/env bash
# setup.sh — one-time setup for Transcribe Studio.
# Creates a venv, installs Flask, validates whisper-cli + ffmpeg.

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

if [[ ! -d venv ]]; then
  echo "→ Creating venv..."
  "$PYTHON" -m venv venv
fi

echo "→ Installing dependencies..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
echo "  Flask installed."

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
