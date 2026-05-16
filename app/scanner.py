"""Scan project folders for source media files and merge with project state."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Optional

from .engine import ALL_EXT, Engine, WhisperConfig
from .projects import Project, ProjectState


def scan_project(project: Project, state: "Optional[ProjectState]" = None) -> list[dict]:
    """Return one record per source file across all project folders.

    Each record:
      {
        path: absolute path,
        name: basename,
        folder: parent folder path,
        size_bytes: int,
        mtime: float (unix timestamp),
        duration_sec: 0 (filled lazily by UI on demand),
        has_transcript: bool,
        transcript_path: optional str,
        source: "folder" | "youtube",  # where this file came from
        youtube_mode: "speech" | "music" | None,  # only set for youtube source
      }

    Pass state so youtube-sourced files get annotated with their URL job's mode
    ("speech" or "music"). This powers the source-type tab filtering in the UI.
    """
    rows: list[dict] = []

    # Build a folder→mode lookup from URL jobs so youtube files can carry their mode.
    _yt_folder_mode: dict[str, str] = {}
    if state is not None:
        for url_row in state.list_urls():
            fp = url_row.get("folder")
            mode = url_row.get("mode")
            if fp and mode:
                _yt_folder_mode[fp] = mode

    # Existing behavior: top-level iterdir of every configured folder.
    for folder in project.folders:
        f = Path(folder).expanduser()
        if not f.exists():
            continue
        for entry in f.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in ALL_EXT:
                continue
            if any(fnmatch.fnmatch(entry.name, pat) for pat in project.exclude_patterns):
                continue
            tx_path = Engine.transcript_path_for(entry, project.config, "txt")
            stat = entry.stat()
            rows.append({
                "path": str(entry),
                "name": entry.name,
                "folder": str(f),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "has_transcript": tx_path.exists(),
                "transcript_path": str(tx_path) if tx_path.exists() else None,
                "source": "folder",
                "youtube_mode": None,
            })

    # YouTube ingest: walk one extra level into <folders[0]>/<youtube_subdir>/<title>/
    # so files produced by yt_ingest become scannable by the existing pipeline.
    # transcriptions/ subdirs are naturally skipped because they're dirs not files.
    if project.youtube_enabled and project.folders:
        yt_root = Path(project.folders[0]).expanduser() / project.youtube_subdir
        if yt_root.exists():
            for url_folder in yt_root.iterdir():
                if not url_folder.is_dir():
                    continue
                yt_mode = _yt_folder_mode.get(str(url_folder))
                for entry in url_folder.iterdir():
                    if not entry.is_file():
                        continue
                    if entry.suffix.lower() not in ALL_EXT:
                        continue
                    if any(fnmatch.fnmatch(entry.name, pat) for pat in project.exclude_patterns):
                        continue
                    tx_path = Engine.transcript_path_for(entry, project.config, "txt")
                    stat = entry.stat()
                    rows.append({
                        "path": str(entry),
                        "name": entry.name,
                        "folder": str(url_folder),
                        "size_bytes": stat.st_size,
                        "mtime": stat.st_mtime,
                        "has_transcript": tx_path.exists(),
                        "transcript_path": str(tx_path) if tx_path.exists() else None,
                        "source": "youtube",
                        "youtube_mode": yt_mode,  # "speech" | "music" | None
                    })

    return rows


def order_files(rows: list[dict], ordering: str, custom_order: list[str] | None = None) -> list[dict]:
    if ordering == "oldest_first":
        return sorted(rows, key=lambda r: r["mtime"])
    if ordering == "alpha":
        return sorted(rows, key=lambda r: r["name"].lower())
    if ordering == "custom" and custom_order:
        # Stable: rows whose path is in custom_order come first in that order,
        # then any rows not yet in custom_order keep their natural (newest-first) tail.
        index = {p: i for i, p in enumerate(custom_order)}
        keyed = [(index.get(r["path"], None), r) for r in rows]
        in_custom = [r for i, r in keyed if i is not None]
        in_custom.sort(key=lambda r: index[r["path"]])
        rest = sorted([r for i, r in keyed if i is None], key=lambda r: r["mtime"], reverse=True)
        return in_custom + rest
    # default: newest first
    return sorted(rows, key=lambda r: r["mtime"], reverse=True)


def annotate_with_state(rows: list[dict], state: ProjectState) -> list[dict]:
    """Add 'status' to each row based on project state."""
    for r in rows:
        r["status"] = state.get_status_for(r["path"])
        if r["has_transcript"] and r["status"] == "pending":
            # The .txt exists on disk but state.json doesn't know — backfill
            r["status"] = "completed"
    return rows


def next_pending(project: Project, state: ProjectState) -> Optional[dict]:
    """The next file the worker should pick up for this project, or None."""
    rows = annotate_with_state(scan_project(project, state), state)

    # 1) Priority queue (user-picked) — first match that's not done
    pq = state.priority_queue()
    for path in pq:
        for r in rows:
            if r["path"] == path and r["status"] in ("pending", "queued", "failed"):
                return r

    # 2) Natural order (or user-chosen custom order), first non-completed/non-skipped
    custom = state.custom_file_order() if project.ordering == "custom" else None
    for r in order_files(rows, project.ordering, custom):
        if r["status"] in ("pending", "queued"):
            return r
    # Optionally: retry failed (capped attempts) — let UI decide
    return None
