"""Background worker — picks the next file across projects and runs the engine."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import conditions
from .engine import Engine, Event, WhisperConfig
from .projects import Project, Registry, ProjectState
from .scanner import next_pending, scan_project, annotate_with_state, order_files


# How often to re-evaluate projects when nothing's pending or conditions
# aren't met. Configurable via UI.
DEFAULT_IDLE_SLEEP = 30
DEFAULT_RESCAN_SEC = 300  # 5 min — also rebuilds the file list from disk


class Worker:
    """One worker handles all projects sequentially (concurrency=1)."""

    def __init__(self, registry: Registry, log_path: Path):
        self.registry = registry
        self.log_path = log_path
        self.engine = Engine()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()

        # Live status (read by the UI)
        self._current_event: Optional[Event] = None
        self._current_project: Optional[str] = None
        self._current_started_at: Optional[float] = None
        self._current_log = deque(maxlen=200)  # last 200 lines from whisper-cli
        self._last_rescan_at: float = 0
        self._idle_sleep = DEFAULT_IDLE_SLEEP

        self._global_paused = False
        self._not_running_reason: str = "starting"

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="transcribe-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.engine.cancel()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self):
        """Force the loop to re-evaluate immediately."""
        self._wake.set()

    # -- pause / resume ----------------------------------------------------

    def pause(self):
        with self._lock:
            self._global_paused = True
        self.engine.cancel()

    def resume(self):
        with self._lock:
            self._global_paused = False
        self.wake()

    @property
    def paused(self) -> bool:
        return self._global_paused

    # -- live status -------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            ev = self._current_event
            elapsed = time.time() - self._current_started_at if self._current_started_at else 0
        return {
            "paused": self._global_paused,
            "running": ev is not None,
            "current_project": self._current_project,
            "current_event": ev.__dict__ if ev else None,
            "current_elapsed_sec": elapsed,
            "log_tail": list(self._current_log),
            "last_rescan_at": self._last_rescan_at,
            "not_running_reason": self._not_running_reason,
            "ac_power": conditions.on_ac_power(),
            "battery_percent": conditions.battery_percent(),
        }

    # -- main loop ---------------------------------------------------------

    def _log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}  {msg}"
        try:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _set_reason(self, reason: str):
        with self._lock:
            if self._not_running_reason != reason:
                self._not_running_reason = reason
                self._log(f"WAITING: {reason}")

    def _conditions_met(self, project: Project) -> tuple[bool, str]:
        if self._global_paused:
            return False, "globally paused"
        if not project.auto_run:
            return False, f"project '{project.name}' has auto_run=off"
        if project.require_ac_power and not conditions.on_ac_power():
            return False, "laptop is on battery"
        for vol in project.required_volumes:
            if not conditions.volume_mounted(vol):
                return False, f"required volume not mounted: {vol}"
        return True, ""

    def _loop(self):
        self._log("worker started")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"worker error: {type(e).__name__}: {e}")
                time.sleep(self._idle_sleep)
                continue

            # Sleep until the next idle interval, OR until woken
            self._wake.wait(timeout=self._idle_sleep)
            self._wake.clear()
        self._log("worker stopped")

    def _tick(self):
        """One iteration: pick a candidate file, transcribe it, or sleep."""
        # Iterate projects in registration order; first one with pending work
        # AND met conditions wins.
        candidate: Optional[tuple[Project, ProjectState, dict]] = None
        first_blocked_reason: Optional[str] = None

        for project in self.registry.all():
            ok, reason = self._conditions_met(project)
            if not ok:
                if first_blocked_reason is None:
                    first_blocked_reason = reason
                continue

            state = self.registry.state(project.id)
            row = next_pending(project, state)
            if row:
                candidate = (project, state, row)
                break

        if candidate is None:
            self._set_reason(first_blocked_reason or "nothing pending")
            return

        project, state, row = candidate
        self._set_reason("running")
        self._process(project, state, row["path"])

    def _process(self, project: Project, state: ProjectState, path: str):
        with self._lock:
            self._current_project = project.id
            self._current_started_at = time.time()
            self._current_log.clear()
            self._current_event = Event("preflight", Path(path).name, {})

        state.mark_in_progress(path)
        self._log(f"START [{project.id}] {path}")

        try:
            for event in self.engine.transcribe(path, project.config):
                with self._lock:
                    self._current_event = event
                if event.phase == "log":
                    line = event.payload.get("line", "")
                    if line:
                        with self._lock:
                            self._current_log.append(line)
                elif event.phase == "done":
                    state.mark_completed(path, duration_sec=event.payload.get("duration_sec", 0))
                    self._log(f"DONE  [{project.id}] {path}")
                elif event.phase == "fail":
                    state.mark_failed(path, event.payload.get("reason", "unknown"))
                    self._log(f"FAIL  [{project.id}] {path}  ({event.payload.get('reason')})")
                elif event.phase == "skip":
                    # idempotent skip: e.g. .txt already existed
                    state.mark_completed(path)
                    self._log(f"SKIP  [{project.id}] {path}  ({event.payload.get('reason')})")
        except Exception as e:
            state.mark_failed(path, f"{type(e).__name__}: {e}")
            self._log(f"FAIL  [{project.id}] {path}  ({e})")
        finally:
            with self._lock:
                self._current_project = None
                self._current_started_at = None
                self._current_event = None
            state.clear_in_progress()
