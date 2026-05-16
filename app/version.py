"""Build version helper — surfaces git branch + short SHA + dirty marker
in the UI footer so you can tell at a glance which checkout is serving.

Computed once at process start (it's just metadata, not runtime state).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .projects import STUDIO_ROOT


def _git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _compute_version() -> dict:
    # The running checkout is the directory containing app/version.py — NOT
    # STUDIO_ROOT, which is a hard-coded canonical path that may differ
    # from the actual checkout (e.g. when running from a worktree).
    checkout = Path(__file__).resolve().parent.parent
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout) or "unknown"
    sha = _git(["rev-parse", "--short", "HEAD"], checkout) or "?"
    dirty = bool(_git(["status", "--porcelain"], checkout))
    return {
        "branch": branch,
        "sha": sha,
        "dirty": dirty,
        "checkout": str(checkout),
        "canonical": str(STUDIO_ROOT),
        "is_canonical": str(checkout) == str(STUDIO_ROOT),
    }


VERSION = _compute_version()
