"""Run-time condition checks (AC power, mounted volumes, pause flags)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def on_ac_power() -> bool:
    """True if the laptop is plugged into AC power."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "ps"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "AC Power" in out
    except Exception:
        # If we can't read power state (e.g. on a desktop Mac), assume yes.
        return True


def battery_percent() -> Optional[int]:
    """Battery percentage, or None if unavailable."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for token in out.split():
            if token.endswith("%;"):
                return int(token.rstrip("%;"))
            if token.endswith("%"):
                return int(token.rstrip("%"))
    except Exception:
        pass
    return None


def volume_mounted(path: str) -> bool:
    """True if the given path exists and is reachable."""
    p = Path(path).expanduser()
    return p.exists()


def all_volumes_mounted(paths: list[str]) -> bool:
    return all(volume_mounted(p) for p in paths)


def is_idle() -> bool:
    """Heuristic: True if user has been idle (no input) for >5 minutes.

    We use ioreg to read HIDIdleTime. Returns False if it can't be determined.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                # value is in nanoseconds
                ns = int(line.split("=")[-1].strip())
                seconds = ns / 1_000_000_000
                return seconds > 300
    except Exception:
        pass
    return False
