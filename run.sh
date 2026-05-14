#!/usr/bin/env bash
# run.sh — entry point invoked by launchd / .app / Terminal.
# Activates venv, sets a TCC-safe CWD (/tmp), and runs the Flask app.
# Sets PYTHONPATH explicitly so `python -m app.main` resolves the package
# from the studio root even though CWD is /tmp.
#
# Venv location: ~/Library/Application Support/transcribe-studio/venv
# (NOT the project root). macOS TCC protects ~/Documents at the kernel
# level, and the venv's python3 symlink resolves to Python.framework's
# binary, which doesn't inherit Full Disk Access from /bin/bash. Putting
# pyvenv.cfg + site-packages in ~/Library/Application Support sidesteps
# TCC entirely. See setup.sh / migrate-venv.command for the migration.

set -euo pipefail

STUDIO="$HOME/Documents/cowork-tools/transcribe-studio"

# Resolve venv location: explicit env override > new location > legacy fallback.
DEFAULT_VENV="$HOME/Library/Application Support/transcribe-studio/venv"
LEGACY_VENV="$STUDIO/venv"
VENV_DIR="${TS_VENV_DIR:-}"
if [[ -z "$VENV_DIR" ]]; then
  if [[ -x "$DEFAULT_VENV/bin/python" ]]; then
    VENV_DIR="$DEFAULT_VENV"
  elif [[ -x "$LEGACY_VENV/bin/python" ]]; then
    VENV_DIR="$LEGACY_VENV"
  else
    VENV_DIR="$DEFAULT_VENV"
  fi
fi

PYTHON="$VENV_DIR/bin/python"
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
