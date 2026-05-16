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
from .yt_ingest import YtIngest, YtIngestConfig, YtIngestError


# How often to re-evaluate projects when nothing's pending or conditions
# aren't met. The 5-minute periodic rescan lives in app/watcher.py now.
DEFAULT_IDLE_SLEEP = 30


class Worker:
    """One worker handles all projects sequentially (concurrency=1)."""

    def __init__(self, registry: Registry, log_path: Path):
        self.registry = registry
        self.log_path = log_path
        self.engine = Engine()
        self.yt_ingest = YtIngest()

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
        self.yt_ingest.cancel()
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
        self.yt_ingest.cancel()

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
        """One iteration: pick a candidate (URL job OR file), run it, or sleep.

        Selection order, per spec decision #3 / "URL jobs run before file jobs
        for the same project":
          1. First project (in registration order) with conditions met AND a
             queued URL gets its URL job processed.
          2. Otherwise, first project with conditions met AND a pending file
             gets that file transcribed (existing behavior).

        The URL job, once complete, leaves the resulting audio file on disk
        where the scanner picks it up; the next tick treats it as a normal
        file and goes through the existing transcribe path.
        """
        url_candidate: Optional[tuple[Project, ProjectState, dict]] = None
        file_candidate: Optional[tuple[Project, ProjectState, dict]] = None
        first_blocked_reason: Optional[str] = None

        for project in self.registry.all():
            ok, reason = self._conditions_met(project)
            if not ok:
                if first_blocked_reason is None:
                    first_blocked_reason = reason
                continue

            state = self.registry.state(project.id)

            # Prefer URL jobs — they "feed" the file pipeline, so getting
            # them through quickly keeps the queue moving.
            if url_candidate is None:
                url_row = state.next_pending_url()
                if url_row is not None:
                    url_candidate = (project, state, url_row)
                    break  # URL job wins immediately; don't scan more projects

            if file_candidate is None:
                row = next_pending(project, state)
                if row:
                    file_candidate = (project, state, row)
                    # don't break — a later project might have a URL job

        if url_candidate is not None:
            self._set_reason("running URL job")
            project, state, url_row = url_candidate
            self._process_url_job(project, state, url_row)
            return

        if file_candidate is not None:
            project, state, row = file_candidate
            self._set_reason("running")
            self._process(project, state, row["path"])
            return

        self._set_reason(first_blocked_reason or "nothing pending")

    def _process(self, project: Project, state: ProjectState, path: str):
        with self._lock:
            self._current_project = project.id
            self._current_started_at = time.time()
            self._current_log.clear()
            self._current_event = Event("preflight", Path(path).name, {})

        state.mark_in_progress(path)
        self._log(f"START [{project.id}] {path}")

        # In music mode, force --no-context if the project has that knob on —
        # it's the proven fix for whisper hallucination loops on vocal stems.
        config_for_run = project.config
        if (project.youtube_enabled
                and project.music_force_no_context
                and self._is_music_target(project, state, path)
                and not project.config.no_context):
            from dataclasses import replace
            config_for_run = replace(project.config, no_context=True)

        try:
            for event in self.engine.transcribe(path, config_for_run):
                with self._lock:
                    self._current_event = event
                if event.phase == "log":
                    line = event.payload.get("line", "")
                    if line:
                        with self._lock:
                            self._current_log.append(line)
                elif event.phase == "done":
                    state.mark_completed(path, duration_sec=event.payload.get("duration_sec", 0))
                    self._close_url_for_target(state, path, status="done")
                    self._log(f"DONE  [{project.id}] {path}")
                    # >>> LOCAL LLM CALL — trigger Ollama analysis if enabled <<<
                    # qwen2.5-coder:14b reads the .txt and writes .analysis.json
                    self._maybe_analyze(project, config_for_run, path)
                elif event.phase == "fail":
                    reason = event.payload.get("reason", "unknown")
                    state.mark_failed(path, reason)
                    self._close_url_for_target(state, path, status="failed", reason=reason)
                    self._log(f"FAIL  [{project.id}] {path}  ({reason})")
                elif event.phase == "skip":
                    # idempotent skip: e.g. .txt already existed
                    state.mark_completed(path)
                    self._close_url_for_target(state, path, status="done")
                    self._log(f"SKIP  [{project.id}] {path}  ({event.payload.get('reason')})")
        except Exception as e:
            state.mark_failed(path, f"{type(e).__name__}: {e}")
            self._close_url_for_target(state, path, status="failed", reason=str(e))
            self._log(f"FAIL  [{project.id}] {path}  ({e})")
        finally:
            with self._lock:
                self._current_project = None
                self._current_started_at = None
                self._current_event = None
            state.clear_in_progress()

    # ---- URL ingest path ----------------------------------------------------

    def _process_url_job(self, project: Project, state: ProjectState, url_row: dict):
        """Run yt_ingest for one URL row, hand the resulting file off to the
        existing transcribe path via the priority queue.
        """
        url_id = url_row["id"]
        url = url_row["url"]
        mode = url_row.get("mode") or project.youtube_default_mode

        # Pre-flight: must have somewhere to write
        if not project.folders:
            state.update_url(
                url_id,
                status="failed", stage=None,
                failed_reason="project has no folders configured",
                finished_at=datetime.now().isoformat(),
            )
            self._log(f"YT_FAIL [{project.id}] {url} — no folders configured")
            return

        dest_root = Path(project.folders[0]).expanduser() / project.youtube_subdir
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            state.update_url(
                url_id,
                status="failed", stage=None,
                failed_reason=f"could not create dest folder: {e}",
                finished_at=datetime.now().isoformat(),
            )
            self._log(f"YT_FAIL [{project.id}] {url} — {e}")
            return

        # Live status setup
        with self._lock:
            self._current_project = project.id
            self._current_started_at = time.time()
            self._current_log.clear()
            self._current_event = Event("preflight_yt", url, {"mode": mode, "url": url})

        state.update_url(
            url_id,
            status="downloading", stage="download",
            started_at=datetime.now().isoformat(),
        )
        self._log(f"YT_START [{project.id}] mode={mode} {url}")

        config = YtIngestConfig(
            mode=mode,
            keep_vocals=project.music_keep_vocals,
            demucs_segment=project.music_demucs_segment,
        )

        final_done_event: Optional[Event] = None
        try:
            for event in self.yt_ingest.ingest(url, dest_root, config):
                with self._lock:
                    self._current_event = event
                if event.phase == "log":
                    line = event.payload.get("line", "")
                    if line:
                        with self._lock:
                            self._current_log.append(line)
                    continue
                if event.phase == "preflight_yt":
                    # second preflight carries the resolved title/folder
                    if event.payload.get("folder"):
                        state.update_url(
                            url_id,
                            title=event.file,
                            video_id=event.payload.get("video_id"),
                            folder=event.payload.get("folder"),
                        )
                elif event.phase == "separate_start":
                    state.update_url(url_id, status="separating", stage="separate")
                elif event.phase == "done":
                    final_done_event = event
                elif event.phase == "fail":
                    reason = event.payload.get("reason", "unknown")
                    state.update_url(
                        url_id,
                        status="failed", stage=None,
                        failed_reason=reason,
                        finished_at=datetime.now().isoformat(),
                    )
                    self._log(f"YT_FAIL [{project.id}] {url} ({reason})")
                    return
        except YtIngestError as e:
            state.update_url(
                url_id,
                status="failed", stage=None,
                failed_reason=str(e),
                finished_at=datetime.now().isoformat(),
            )
            self._log(f"YT_FAIL [{project.id}] {url} ({e})")
            return
        except Exception as e:
            state.update_url(
                url_id,
                status="failed", stage=None,
                failed_reason=f"{type(e).__name__}: {e}",
                finished_at=datetime.now().isoformat(),
            )
            self._log(f"YT_FAIL [{project.id}] {url} ({e})")
            return
        finally:
            with self._lock:
                self._current_project = None
                self._current_started_at = None
                self._current_event = None

        # If we got here without final_done_event, the generator ended
        # without a terminal event — shouldn't happen but treat as fail.
        if final_done_event is None:
            state.update_url(
                url_id,
                status="failed", stage=None,
                failed_reason="ingest ended without 'done' event",
                finished_at=datetime.now().isoformat(),
            )
            return

        # Ingest succeeded. Hand off to the transcribe path.
        target_file = final_done_event.payload.get("output")
        folder = final_done_event.payload.get("folder")
        state.update_url(
            url_id,
            status="transcribing", stage="transcribe",
            target_file=target_file,
        )

        # Push the target file to the front of the queue so it runs next.
        if target_file:
            state.prioritize([target_file])

        # Music mode: explicitly skip the non-target audio files in the same
        # folder so they don't get transcribed (source.mp3 is the original
        # mix; instrumental.mp3 has no vocals → no useful transcript).
        if mode == "music" and folder:
            for fname in ("source.mp3", "instrumental.mp3"):
                other = str(Path(folder) / fname)
                if Path(other).exists() and other != target_file:
                    state.mark_skipped(other)

        self._log(f"YT_INGEST_DONE [{project.id}] {url} -> {target_file}")

    # ---- helpers tying file lifecycle back to URL rows ----------------------

    def _close_url_for_target(self, state: ProjectState, path: str,
                              *, status: str, reason: Optional[str] = None) -> None:
        """If any URL row points at this target file, mark it done/failed."""
        for row in state.list_urls():
            if row.get("target_file") == path and row.get("status") == "transcribing":
                changes = {
                    "status": status,
                    "stage": None,
                    "finished_at": datetime.now().isoformat(),
                }
                if reason:
                    changes["failed_reason"] = reason
                state.update_url(row["id"], **changes)
                self._log(
                    f"YT_{status.upper()} [{state.project.id}] "
                    f"url={row['url']} target={path}"
                    + (f" ({reason})" if reason else "")
                )

    def _is_music_target(self, project: Project, state: ProjectState, path: str) -> bool:
        """True iff `path` is the vocals.mp3 produced by a music-mode URL job
        in this project — i.e. the file we'd want --no-context applied to."""
        for row in state.list_urls():
            if row.get("target_file") == path and row.get("mode") == "music":
                return True
        return False

    def _maybe_analyze(self, project: Project, config, path: str) -> None:
        """Run Ollama analysis on the completed transcript if the project has it enabled.

        >>> LOCAL LLM INTEGRATION POINT <<<
        When project.ollama.enabled is True, this sends the .txt transcript to
        qwen2.5-coder:14b (or whichever model is configured) and writes a
        .analysis.json sidecar. The worker streams analysis Events to _current_event
        so the UI shows "analyzing…" in the live status panel.

        No-op if ollama is disabled (default) — zero overhead for existing projects.
        """
        if not project.ollama.enabled or not project.ollama.model:
            return
        from .engine import Engine as _Engine
        tx_path = _Engine.transcript_path_for(Path(path), config, "txt")
        if not tx_path.exists():
            return
        self._log(f"ANALYZE_START [{project.id}] {tx_path.name} model={project.ollama.model}")
        from .analyzer import analyze_transcript
        try:
            for ev in analyze_transcript(tx_path, project.ollama.model, project.ollama.analyses):
                with self._lock:
                    self._current_event = ev
                if ev.phase == "analyze_done":
                    self._log(f"ANALYZE_DONE [{project.id}] {tx_path.name}")
                elif ev.phase == "fail":
                    self._log(f"ANALYZE_FAIL [{project.id}] {tx_path.name} ({ev.payload.get('reason')})")
        except Exception as e:
            self._log(f"ANALYZE_ERR [{project.id}] {tx_path.name} ({e})")
