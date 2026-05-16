"""Periodic folder watcher — scans every project on an interval and logs
new files. Independent of the Worker so it keeps running even when the
worker is blocked inside a long whisper transcription.

This thread NEVER mutates project state. New files appear as "pending"
to the existing scanner; the worker picks them up on its next tick via
next_pending(). The watcher's job is observability + a wake() nudge so
the user doesn't have to wait up to ~30s for the next worker tick.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .projects import Registry
from .scanner import annotate_with_state, scan_project


DEFAULT_WATCH_SEC = 300  # 5 min


class Watcher:
    def __init__(self, registry: Registry, worker, log_path: Path,
                 interval_sec: int = DEFAULT_WATCH_SEC):
        self.registry = registry
        self.worker = worker
        self.log_path = log_path
        self.interval = interval_sec

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_scan_at: float = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="folder-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_path, "a") as f:
                f.write(f"{ts}  {msg}\n")
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self._log(f"watcher error: {type(e).__name__}: {e}")
            self._wake.wait(timeout=self.interval)
            self._wake.clear()

    def tick(self):
        """One scan pass. Per-project: count files the queue hasn't seen,
        log if non-zero, wake the worker once at the end if anything was
        discovered."""
        any_new = False
        for p in self.registry.all():
            state = self.registry.state(p.id)
            rows = annotate_with_state(scan_project(p, state), state)
            new_paths = [r["path"] for r in rows if r["status"] == "pending"]
            if new_paths:
                self._log(f"WATCH [{p.id}] discovered {len(new_paths)} new files")
                any_new = True
        self.last_scan_at = time.time()
        if any_new:
            self.worker.wake()
