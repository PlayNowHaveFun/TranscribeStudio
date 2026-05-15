"""Project registry — stores the multi-project config and per-project state."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .engine import WhisperConfig, ALL_EXT


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

STUDIO_ROOT = Path.home() / "Documents/cowork-tools/transcribe-studio"
DATA_DIR = STUDIO_ROOT / "data"
PROJECTS_FILE = DATA_DIR / "projects.json"


# --------------------------------------------------------------------------
# Ollama config (per-project AI analysis settings)
# --------------------------------------------------------------------------

@dataclass
class OllamaConfig:
    """Settings for optional local-LLM transcript analysis via Ollama.

    >>> LOCAL LLM INTEGRATION POINT <<<
    When enabled, after each transcription the worker sends the .txt to
    the configured Ollama model (default: qwen2.5-coder:14b) and writes
    a .analysis.json sidecar with summary + topics.

    All fields default to off so existing projects load unchanged.
    """
    enabled: bool = False
    model: str = "qwen2.5-coder:14b"   # matches what's installed via `ollama list`
    analyses: list = field(default_factory=lambda: ["summary", "topics"])
    base_url: str = "http://localhost:11434"


# --------------------------------------------------------------------------
# Project dataclass
# --------------------------------------------------------------------------

@dataclass
class Project:
    id: str                                    # slug, used as folder name
    name: str                                  # display name
    folders: list[str]                         # absolute paths
    config: WhisperConfig                      # whisper params for this project
    auto_run: bool = True                      # process this project's queue automatically
    require_ac_power: bool = True
    required_volumes: list[str] = field(default_factory=list)  # paths that must be mounted
    exclude_patterns: list[str] = field(default_factory=list)  # globs to skip
    ordering: str = "newest_first"             # "newest_first" | "oldest_first" | "alpha"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    notes: str = ""

    # --- YouTube ingest (added per SPEC_YOUTUBE_AND_MUSIC.md) ---
    # All default to "off" so existing projects load unchanged.
    youtube_enabled: bool = False              # gates the URL inbox UI per project
    youtube_default_mode: str = "speech"       # "speech" | "music" — default mode for new URLs
    youtube_subdir: str = "youtube"            # relative to folders[0]
    music_keep_vocals: bool = True             # if False, delete vocals.mp3 after transcription
    music_force_no_context: bool = True        # apply --no-context for music-mode transcription
    music_demucs_segment: int = 0              # 0 = no segmenting; raise to 7 if OOM on long tracks

    # --- Ollama AI analysis (local LLM post-processing) ---
    ollama: OllamaConfig = field(default_factory=OllamaConfig)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["config"] = {**asdict(self.config), "formats": list(self.config.formats)}
        d["ollama"] = asdict(self.ollama)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        cfg_d = d.get("config", {})
        cfg_d["formats"] = tuple(cfg_d.get("formats", ("txt", "srt")))
        cfg = WhisperConfig(**cfg_d)
        # OllamaConfig — .get() with {} default so old projects.json loads unchanged
        ollama_d = d.get("ollama", {})
        ollama_cfg = OllamaConfig(
            enabled=ollama_d.get("enabled", False),
            model=ollama_d.get("model", "qwen2.5-coder:14b"),
            analyses=ollama_d.get("analyses", ["summary", "topics"]),
            base_url=ollama_d.get("base_url", "http://localhost:11434"),
        )
        return cls(
            id=d["id"],
            name=d["name"],
            folders=list(d.get("folders", [])),
            config=cfg,
            auto_run=d.get("auto_run", True),
            require_ac_power=d.get("require_ac_power", True),
            required_volumes=list(d.get("required_volumes", [])),
            exclude_patterns=list(d.get("exclude_patterns", [])),
            ordering=d.get("ordering", "newest_first"),
            created_at=d.get("created_at", datetime.now().isoformat()),
            notes=d.get("notes", ""),
            # YouTube fields — .get() with defaults so old projects.json loads unchanged
            youtube_enabled=d.get("youtube_enabled", False),
            youtube_default_mode=d.get("youtube_default_mode", "speech"),
            youtube_subdir=d.get("youtube_subdir", "youtube"),
            music_keep_vocals=d.get("music_keep_vocals", True),
            music_force_no_context=d.get("music_force_no_context", True),
            music_demucs_segment=d.get("music_demucs_segment", 0),
            ollama=ollama_cfg,
        )

    @property
    def state_dir(self) -> Path:
        d = DATA_DIR / "projects" / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d


# --------------------------------------------------------------------------
# Per-project state (queue, completions, failures, manual priorities)
# --------------------------------------------------------------------------

class ProjectState:
    """Thread-safe persistent state for one project."""

    def __init__(self, project: Project):
        self.project = project
        self.path = project.state_dir / "state.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        loaded: dict = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
            except Exception:
                loaded = {}
        # Defaults — every key gets set if missing so older state.json
        # files (pre-YouTube) self-heal on first load without a migration.
        defaults = {
            "completed": {},      # path -> {completed_at, duration_sec}
            "failed": {},         # path -> {failed_at, reason, attempts}
            "priority_queue": [], # paths to process next regardless of order
            "skipped": {},        # paths the user marked as skip (not pending)
            "current": None,      # currently in progress, if any
            "youtube_urls": [],   # list of url-job dicts (see add_url)
        }
        for k, v in defaults.items():
            loaded.setdefault(k, v)
        return loaded

    def _save(self):
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)

    # -- queries --
    def is_completed(self, path: str) -> bool:
        with self._lock:
            return path in self._data["completed"]

    def is_failed(self, path: str) -> bool:
        with self._lock:
            return path in self._data["failed"]

    def is_skipped(self, path: str) -> bool:
        with self._lock:
            return path in self._data["skipped"]

    def get_status_for(self, path: str) -> str:
        with self._lock:
            if self._data.get("current") == path: return "in_progress"
            if path in self._data["completed"]: return "completed"
            if path in self._data["failed"]:    return "failed"
            if path in self._data["skipped"]:   return "skipped"
            if path in self._data["priority_queue"]: return "queued"
        return "pending"

    def priority_queue(self) -> list[str]:
        with self._lock:
            return list(self._data["priority_queue"])

    def stats(self) -> dict:
        with self._lock:
            return {
                "completed": len(self._data["completed"]),
                "failed":    len(self._data["failed"]),
                "skipped":   len(self._data["skipped"]),
            }

    # -- mutations --
    def mark_in_progress(self, path: str):
        self._data["current"] = path
        self._save()

    def clear_in_progress(self):
        self._data["current"] = None
        self._save()

    def mark_completed(self, path: str, duration_sec: float = 0):
        self._data["current"] = None
        self._data["completed"][path] = {
            "completed_at": datetime.now().isoformat(),
            "duration_sec": duration_sec,
        }
        self._data["failed"].pop(path, None)
        self._data["priority_queue"] = [p for p in self._data["priority_queue"] if p != path]
        self._save()

    def mark_failed(self, path: str, reason: str):
        self._data["current"] = None
        prev = self._data["failed"].get(path, {"attempts": 0})
        self._data["failed"][path] = {
            "failed_at": datetime.now().isoformat(),
            "reason": reason,
            "attempts": prev.get("attempts", 0) + 1,
        }
        self._save()

    def mark_skipped(self, path: str):
        self._data["skipped"][path] = {"skipped_at": datetime.now().isoformat()}
        self._save()

    def unmark(self, path: str):
        """Reset a file to 'pending' so it'll be re-considered by the worker."""
        self._data["completed"].pop(path, None)
        self._data["failed"].pop(path, None)
        self._data["skipped"].pop(path, None)
        self._save()

    def prioritize(self, paths: list[str]):
        with self._lock:
            existing = set(self._data["priority_queue"])
            for p in paths:
                if p not in existing:
                    self._data["priority_queue"].append(p)
        self._save()

    def deprioritize(self, path: str):
        self._data["priority_queue"] = [p for p in self._data["priority_queue"] if p != path]
        self._save()

    # ------------------------------------------------------------------
    # YouTube URL inbox — added per SPEC_YOUTUBE_AND_MUSIC.md
    #
    # Each row is a plain dict (matches the existing "completed"/"failed"
    # patterns in this file, no dataclass required). Schema:
    #   {
    #     id, url, submitted_at, mode,
    #     title?, video_id?,
    #     status,           # queued|downloading|separating|transcribing|done|failed
    #     stage?,           # specific sub-stage when status is in-flight
    #     target_file?,     # absolute path to the file the transcriber will pick up
    #     folder?,          # the per-URL folder under <project>/<youtube_subdir>/
    #     metadata_path?,
    #     failed_reason?,
    #     started_at?, finished_at?,
    #   }
    # ------------------------------------------------------------------

    def list_urls(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("youtube_urls", []))

    def get_url(self, url_id: str) -> Optional[dict]:
        with self._lock:
            for row in self._data.get("youtube_urls", []):
                if row.get("id") == url_id:
                    return dict(row)  # copy so callers can't mutate without going through update_url
        return None

    def add_url(self, url: str, mode: str) -> dict:
        """Append a new URL job to the inbox. Returns the new row dict."""
        row = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "submitted_at": datetime.now().isoformat(),
            "mode": mode,
            "title": None,
            "video_id": None,
            "status": "queued",
            "stage": None,
            "target_file": None,
            "folder": None,
            "metadata_path": None,
            "failed_reason": None,
            "started_at": None,
            "finished_at": None,
            "priority": False,   # "↑ Up next" — sorted ahead of normal queued rows
        }
        with self._lock:
            self._data.setdefault("youtube_urls", []).append(row)
        self._save()
        return dict(row)

    def update_url(self, url_id: str, **changes) -> Optional[dict]:
        """Patch a URL row in place. Returns the updated row or None if not found."""
        with self._lock:
            for row in self._data.get("youtube_urls", []):
                if row.get("id") == url_id:
                    row.update(changes)
                    updated = dict(row)
                    break
            else:
                return None
        self._save()
        return updated

    def remove_url(self, url_id: str) -> bool:
        """Drop a URL row from the inbox. Returns True if a row was found."""
        with self._lock:
            urls = self._data.get("youtube_urls", [])
            new = [r for r in urls if r.get("id") != url_id]
            if len(new) == len(urls):
                return False
            self._data["youtube_urls"] = new
        self._save()
        return True

    def next_pending_url(self) -> Optional[dict]:
        """Next queued URL row for the worker, or None.

        Sort order:
          1. priority=True rows first (in submission order among themselves)
          2. then normal queued rows in submission order

        in-flight statuses (downloading/separating/transcribing) are owned
        by the worker and skipped here. Terminal (done/failed) are skipped.
        """
        with self._lock:
            queued = [
                dict(r) for r in self._data.get("youtube_urls", [])
                if r.get("status") == "queued"
            ]
        if not queued:
            return None
        # Stable sort: priority desc, then submitted_at asc (insertion order).
        queued.sort(key=lambda r: (
            0 if r.get("priority") else 1,
            r.get("submitted_at") or "",
        ))
        return queued[0]


# --------------------------------------------------------------------------
# Registry — the file-backed list of all projects
# --------------------------------------------------------------------------

class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, Project] = {}
        self._states: dict[str, ProjectState] = {}
        self._load()

    def _load(self):
        if PROJECTS_FILE.exists():
            try:
                data = json.loads(PROJECTS_FILE.read_text())
                for d in data.get("projects", []):
                    p = Project.from_dict(d)
                    self._projects[p.id] = p
                    self._states[p.id] = ProjectState(p)
            except Exception as e:
                print(f"Failed to load projects.json: {e}")

    def _save(self):
        data = {"projects": [p.to_dict() for p in self._projects.values()]}
        tmp = PROJECTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(PROJECTS_FILE)

    def all(self) -> list[Project]:
        with self._lock:
            return list(self._projects.values())

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            return self._projects.get(project_id)

    def state(self, project_id: str) -> Optional[ProjectState]:
        with self._lock:
            return self._states.get(project_id)

    def add(self, project: Project) -> Project:
        with self._lock:
            self._projects[project.id] = project
            self._states[project.id] = ProjectState(project)
            self._save()
        return project

    def update(self, project_id: str, **changes) -> Optional[Project]:
        with self._lock:
            p = self._projects.get(project_id)
            if not p:
                return None
            for k, v in changes.items():
                if k == "config" and isinstance(v, dict):
                    cfg_d = {**asdict(p.config), **v}
                    cfg_d["formats"] = tuple(cfg_d.get("formats", ("txt", "srt")))
                    p.config = WhisperConfig(**cfg_d)
                elif hasattr(p, k):
                    setattr(p, k, v)
            self._save()
            return p

    def delete(self, project_id: str) -> bool:
        with self._lock:
            if project_id not in self._projects:
                return False
            del self._projects[project_id]
            self._states.pop(project_id, None)
            self._save()
        return True


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or f"project-{uuid.uuid4().hex[:8]}"
