#!/usr/bin/env bash
# run.sh — entry point invoked by launchd / .app / Terminal.
# Activates venv, sets a TCC-safe CWD (/tmp), and runs the Flask app.
# Sets PYTHONPATH explicitly so `python -m app.main` resolves the package
# from the studio root even though CWD is /tmp.

set -euo pipefail

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"
PYTHON="$STUDIO/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "✗ venv not initialized at $PYTHON. Run setup.sh first." >&2
  exit 1
fi

mkdir -p "$STUDIO/logs"

# CWD = /tmp so child binaries (whisper-cli, ffmpeg) don't trip TCC on getcwd.
cd /tmp

# Tell Python where the `app` package lives.
export PYTHONPATH="$STUDIO"

exec "$PYTHON" -m app.main "$@"
