"""
YouTube ingest — Gate 2 prototype.

Pulls audio from a YouTube URL via yt-dlp, optionally separates vocals
from instrumental via Demucs. Does NOT transcribe — once audio lands
on disk, the existing engine.py picks it up through the normal
scanner path (Gate 3 will wire that in).

Decisions baked in here (see GATE_CHECKIN.md, decisions log):
  * #9  Python module only, no standalone CLI surface.
  * #10 Caller owns the destination folder; this module never reaches
        into other projects to dedupe.
  * #11 Routes that wrap this module must keep clean JSON in/out so
        the MCP wrapping track can lift them as-is.

Patterns mirrored from engine.py so Gate 3 integration is trivial:
  * Yields engine.Event objects with new `phase` values:
      preflight_yt, download_start, download_progress, download_done,
      separate_start, separate_progress, separate_done,
      done, fail, log
  * Typed exceptions in the EngineError tree where it makes sense.
  * Subprocess wrapper runs at low priority with caffeinate, cwd=/tmp
    to avoid the launchd TCC trap that bit the original setup.
  * Cancellation through a threading.Event + tracked Popen handle.

Binaries this shells out to:
  - yt-dlp        (`brew install yt-dlp`)
  - demucs        (`pip install demucs` — pulls torch, ~2 GB first time)

Both checks are lazy: missing binaries surface as typed errors so the
UI can show an install CTA without crashing the worker.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterator, Optional

from .engine import Event, EngineError


# --------------------------------------------------------------------------
# Exceptions — slot under EngineError so the worker's existing handler
# tree catches them with no changes.
# --------------------------------------------------------------------------

class YtIngestError(EngineError): pass
class MissingYtDlpError(YtIngestError): pass
class MissingDemucsError(YtIngestError): pass
class YtDlpFailedError(YtIngestError): pass
class DemucsFailedError(YtIngestError): pass
class InvalidMetadataError(YtIngestError): pass
class NotAPlaylistError(YtIngestError): pass


# --------------------------------------------------------------------------
# Config — caller-supplied options for one ingest run.
# --------------------------------------------------------------------------

@dataclass
class YtIngestConfig:
    mode: str = "speech"          # "speech" | "music"
    audio_bitrate: str = "320"    # mp3 quality
    keep_vocals: bool = True      # delete vocals.mp3 after caller is done?
                                  # this module doesn't delete; caller does.
                                  # carried here so it round-trips through
                                  # any persistence layer.
    demucs_segment: int = 0       # 0 = no segmenting; bump to 7 if OOM
    demucs_model: str = "htdemucs"


# --------------------------------------------------------------------------
# Title sanitization & collision handling
# --------------------------------------------------------------------------

# macOS / common filesystems reject these in filenames; we also strip
# leading dots so we never produce hidden folders.
_BAD_CHARS_RE = re.compile(r'[\/\\:*?"<>|\x00-\x1f]+')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_title(title: str, max_len: int = 180) -> str:
    """Turn a YouTube video title into a filesystem-safe folder name.

    Conservative: replaces problem characters with '-', collapses
    whitespace, strips leading/trailing dots and dashes, caps length.
    Empty / pathological inputs collapse to 'untitled'.
    """
    if not title:
        return "untitled"
    s = _BAD_CHARS_RE.sub("-", title)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = s.strip(".- ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(".- ")
    return s or "untitled"


def target_folder(dest_root: Path, title: str, video_id: str) -> Path:
    """Compute the folder a URL should land in, applying collision
    disambiguation (append a 6-char hash of the video_id).

    Pure function — does not create anything on disk."""
    sanitized = sanitize_title(title)
    folder = dest_root / sanitized
    if folder.exists():
        h = hashlib.sha1(video_id.encode("utf-8")).hexdigest()[:6]
        folder = dest_root / f"{sanitized}_{h}"
    return folder


# --------------------------------------------------------------------------
# YtIngest — class with cancel state, mirrors engine.Engine in shape
# --------------------------------------------------------------------------

class YtIngest:
    """Stateless orchestrator (apart from cancel flag).
    One instance can be reused for many URLs."""

    def __init__(self):
        self._cancel_flag = threading.Event()
        self._current_proc: Optional[subprocess.Popen] = None

    # -- binary discovery -------------------------------------------------

    @staticmethod
    def find_yt_dlp() -> str:
        # yt-dlp is installed via brew, so it lives on the system PATH.
        # But launchd's plist scrubs PATH down to a known set — explicitly
        # check the common brew locations first so we don't rely on env.
        for candidate in ("/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
            if Path(candidate).is_file():
                return candidate
        path = shutil.which("yt-dlp")
        if not path:
            raise MissingYtDlpError(
                "yt-dlp not found. Install with: brew install yt-dlp"
            )
        return path

    @staticmethod
    def find_demucs() -> str:
        # demucs is a pip-installed Python script that lives in <venv>/bin/.
        # sys.prefix points at the venv root when we're running inside one,
        # which we always are under launchd. Check there first — PATH lookup
        # is unreliable because launchd's PATH doesn't include the venv bin.
        venv_demucs = Path(sys.prefix) / "bin" / "demucs"
        if venv_demucs.is_file():
            return str(venv_demucs)
        path = shutil.which("demucs")
        if not path:
            raise MissingDemucsError(
                "demucs not found. Install into the studio venv with: "
                "pip install demucs"
            )
        return path

    # -- cancellation -----------------------------------------------------

    def cancel(self):
        self._cancel_flag.set()
        proc = self._current_proc
        if proc:
            try: proc.terminate()
            except Exception: pass

    def reset_cancel(self):
        self._cancel_flag.clear()

    # -- subprocess runner — copy of engine._run, kept local so the two
    # don't end up tangled by a shared abstraction we'd then have to keep
    # in sync.
    # --------------------------------------------------------------------

    def _run(self, cmd: list[str], cwd: str = "/tmp") -> Iterator[str]:
        full = ["nice", "-n", "19", "caffeinate", "-i"] + cmd
        proc = subprocess.Popen(
            full,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            cwd=cwd,
        )
        self._current_proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._cancel_flag.is_set():
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill()
                    return
                yield line.rstrip("\n")
            proc.wait()
        finally:
            self._current_proc = None
        if proc.returncode != 0:
            # Generic — the caller stage maps this to a stage-specific
            # error in the Event payload, so we don't need a separate
            # class per binary here. Was YtDlpFailedError; renamed to
            # the base class because this helper is shared with demucs
            # and the old name was actively misleading on failure.
            raise YtIngestError(
                f"subprocess exited with {proc.returncode}: {' '.join(cmd[:3])}…"
            )

    # -- metadata fetch ---------------------------------------------------

    def fetch_metadata(self, url: str) -> dict:
        """Call yt-dlp --skip-download --print-json. Returns the trimmed
        dict we'll persist, NOT the full yt-dlp blob (which is huge)."""
        bin_ = self.find_yt_dlp()
        # Use subprocess.run here, not the streaming _run — we want the
        # full JSON blob in one shot, not line-by-line.
        result = subprocess.run(
            [bin_, "--skip-download", "--print-json", "--no-warnings", url],
            capture_output=True, text=True, timeout=60, cwd="/tmp",
        )
        if result.returncode != 0:
            raise YtDlpFailedError(
                f"yt-dlp metadata fetch failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:500]}"
            )
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise InvalidMetadataError(f"yt-dlp returned non-JSON: {e}")
        return {
            "id":            raw.get("id"),
            "title":         raw.get("title", ""),
            "uploader":      raw.get("uploader"),
            "uploader_url":  raw.get("uploader_url"),
            "channel":       raw.get("channel"),
            "duration":      raw.get("duration"),
            "upload_date":   raw.get("upload_date"),
            "webpage_url":   raw.get("webpage_url") or url,
            "thumbnail":     raw.get("thumbnail"),
            "description":   (raw.get("description") or "")[:1000],
            "tags":          raw.get("tags", []),
            "ext":           raw.get("ext"),
        }

    # -- download stage ---------------------------------------------------

    def _download_audio(self, url: str, dest_folder: Path) -> Iterator[str]:
        bin_ = self.find_yt_dlp()
        # -x = extract audio; quality 0 = best in mp3 land at our bitrate
        cmd = [
            bin_,
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-warnings",
            "-o", str(dest_folder / "source.%(ext)s"),
            url,
        ]
        yield from self._run(cmd)

    # -- separation stage -------------------------------------------------

    def _separate_vocals(self, source_mp3: Path, dest_folder: Path,
                         config: YtIngestConfig) -> Iterator[str]:
        bin_ = self.find_demucs()
        work = dest_folder / "_demucs"
        work.mkdir(parents=True, exist_ok=True)
        cmd = [
            bin_,
            "--two-stems=vocals",
            "--mp3", "--mp3-bitrate", config.audio_bitrate,
            "-n", config.demucs_model,
            "-o", str(work),
        ]
        if config.demucs_segment > 0:
            cmd += ["--segment", str(config.demucs_segment)]
        cmd.append(str(source_mp3))
        yield from self._run(cmd)

    @staticmethod
    def _promote_stems(work_root: Path, dest_folder: Path,
                       config: YtIngestConfig) -> None:
        """Demucs writes to `<work>/<model>/<input_stem>/{vocals,no_vocals}.mp3`.
        Hoist them up to the project folder with friendly names, then
        delete `_demucs/`."""
        model_dir = work_root / config.demucs_model
        if not model_dir.exists():
            raise DemucsFailedError(
                f"demucs produced no output under {model_dir} — check logs"
            )
        # there's exactly one subdir under model_dir, named after the
        # input file's stem
        subdirs = [d for d in model_dir.iterdir() if d.is_dir()]
        if not subdirs:
            raise DemucsFailedError(f"no stem dir under {model_dir}")
        stem_dir = subdirs[0]

        vocals_src = stem_dir / "vocals.mp3"
        instr_src  = stem_dir / "no_vocals.mp3"
        if not vocals_src.exists() or not instr_src.exists():
            raise DemucsFailedError(
                f"demucs output missing expected files in {stem_dir}: "
                f"{[p.name for p in stem_dir.iterdir()]}"
            )
        shutil.move(str(vocals_src), str(dest_folder / "vocals.mp3"))
        shutil.move(str(instr_src),  str(dest_folder / "instrumental.mp3"))
        shutil.rmtree(work_root, ignore_errors=True)

    # -- top-level ingest -------------------------------------------------

    def ingest(
        self,
        url: str,
        dest_root: Path,
        config: YtIngestConfig,
    ) -> Generator[Event, None, None]:
        """Run the full ingest pipeline for one URL.

        Yields engine.Event so the (future) worker can stream live status
        through the same channel transcription already uses.
        """
        self.reset_cancel()
        dest_root = Path(dest_root).expanduser().resolve()
        dest_root.mkdir(parents=True, exist_ok=True)

        # ---- preflight: binary checks + metadata fetch ----
        try:
            self.find_yt_dlp()
            if config.mode == "music":
                self.find_demucs()
        except (MissingYtDlpError, MissingDemucsError) as e:
            yield Event("fail", url, {"reason": str(e), "stage": "preflight_yt"})
            return

        yield Event("preflight_yt", url, {"mode": config.mode})
        try:
            meta = self.fetch_metadata(url)
        except YtIngestError as e:
            yield Event("fail", url, {"reason": str(e), "stage": "metadata"})
            return

        title = meta.get("title") or "untitled"
        vid_id = meta.get("id") or hashlib.sha1(url.encode()).hexdigest()[:11]
        folder = target_folder(dest_root, title, vid_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2))

        yield Event("preflight_yt", title, {
            "video_id": vid_id,
            "folder": str(folder),
            "duration": meta.get("duration"),
        })

        # ---- download ----
        yield Event("download_start", title, {"folder": str(folder)})
        try:
            for line in self._download_audio(url, folder):
                if line.strip():
                    yield Event("log", title, {"line": line, "stage": "download"})
                    # yt-dlp emits "[download]  47.3% of 5.12MiB at ..." lines;
                    # parse them opportunistically for the UI without making
                    # the whole pipeline depend on the format.
                    pct = _parse_yt_dlp_percent(line)
                    if pct is not None:
                        yield Event("download_progress", title, {"percent": pct})
        except YtIngestError as e:
            yield Event("fail", title, {"reason": str(e), "stage": "download"})
            return

        source_mp3 = folder / "source.mp3"
        if not source_mp3.exists():
            yield Event("fail", title, {
                "reason": "yt-dlp completed but source.mp3 missing",
                "stage": "download",
            })
            return
        yield Event("download_done", title, {"output": str(source_mp3)})

        # ---- speech mode: we're done ----
        if config.mode == "speech":
            yield Event("done", title, {
                "output": str(source_mp3),
                "folder": str(folder),
                "mode": "speech",
            })
            return

        # ---- music mode: separate ----
        yield Event("separate_start", title, {"input": str(source_mp3)})
        try:
            for line in self._separate_vocals(source_mp3, folder, config):
                if line.strip():
                    yield Event("log", title, {"line": line, "stage": "separate"})
                    pct = _parse_demucs_percent(line)
                    if pct is not None:
                        yield Event("separate_progress", title, {"percent": pct})
        except YtIngestError as e:
            yield Event("fail", title, {"reason": str(e), "stage": "separate"})
            return

        try:
            self._promote_stems(folder / "_demucs", folder, config)
        except DemucsFailedError as e:
            yield Event("fail", title, {"reason": str(e), "stage": "separate"})
            return

        vocals_mp3 = folder / "vocals.mp3"
        instr_mp3  = folder / "instrumental.mp3"
        yield Event("separate_done", title, {
            "vocals": str(vocals_mp3),
            "instrumental": str(instr_mp3),
        })

        yield Event("done", title, {
            "output": str(vocals_mp3),   # the file the transcriber will pick up
            "folder": str(folder),
            "mode": "music",
            "vocals": str(vocals_mp3),
            "instrumental": str(instr_mp3),
        })


# --------------------------------------------------------------------------
# Public-playlist metadata fetch — yt-dlp, no API key, no OAuth.
#
# Single-shot helper (not part of YtIngest because it doesn't ingest audio,
# just enumerates a playlist's items). Used by /api/projects/from-playlist
# to turn a pasted playlist URL into a list of video URLs we can enqueue.
#
# Pagination caveat: --flat-playlist tells yt-dlp not to resolve each item,
# so the returned `entries[].url` is just the video URL string. Per-entry
# metadata is sparse on purpose — full metadata gets fetched at ingest time
# by YtIngest.fetch_metadata.
# --------------------------------------------------------------------------

def fetch_playlist_metadata(playlist_url: str, timeout_sec: int = 120) -> dict:
    """Enumerate a public YouTube playlist's items via yt-dlp.

    Returns:
        {
          "playlist_id": str | None,
          "title":       str,
          "uploader":    str | None,
          "item_count":  int,                  # entries actually returned
          "items": [
            {
              "url":          str,             # canonical video URL
              "video_id":     str | None,
              "title":        str,
              "uploader":     str | None,
              "duration":     float | None,    # seconds; None on flat-playlist
              "availability": str | None,      # "public" | "private" | "unlisted" | "needs_auth" | ...
            },
            ...
          ],
        }

    Raises:
        MissingYtDlpError — yt-dlp not installed.
        YtDlpFailedError  — yt-dlp exited non-zero (network error, bad URL, etc).
        InvalidMetadataError — yt-dlp returned non-JSON or no entries.
        NotAPlaylistError — URL pointed at a single video, not a playlist.
    """
    bin_ = YtIngest.find_yt_dlp()
    result = subprocess.run(
        [
            bin_,
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            playlist_url,
        ],
        capture_output=True, text=True, timeout=timeout_sec, cwd="/tmp",
    )
    if result.returncode != 0:
        raise YtDlpFailedError(
            f"yt-dlp playlist fetch failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise InvalidMetadataError(f"yt-dlp returned non-JSON: {e}")

    # When given a non-playlist URL, yt-dlp returns the single video's
    # metadata blob (no "entries" key). Reject explicitly — the caller is
    # the playlist-creation flow, not the single-URL ingest flow.
    if raw.get("_type") != "playlist" and "entries" not in raw:
        raise NotAPlaylistError(
            "URL is not a YouTube playlist. Paste a playlist URL "
            "(e.g. one containing ?list=PL... )."
        )

    entries = raw.get("entries") or []
    items: list[dict] = []
    for e in entries:
        if not e:
            # yt-dlp emits None entries for items it couldn't even peek at
            # (truly private/deleted/blocked-from-the-flat-view). Track but
            # don't include in items[] — caller computes skipped count from
            # the difference between raw entries and items.
            continue
        items.append({
            "url":          e.get("url") or e.get("webpage_url") or "",
            "video_id":     e.get("id"),
            "title":        e.get("title") or "(untitled)",
            "uploader":     e.get("uploader") or e.get("channel"),
            "duration":     e.get("duration"),
            "availability": e.get("availability"),
        })

    if not items and not entries:
        raise InvalidMetadataError("Playlist contains no entries")

    return {
        "playlist_id": raw.get("id"),
        "title":       raw.get("title") or "Untitled playlist",
        "uploader":    raw.get("uploader") or raw.get("channel"),
        "item_count":  len(items),
        "raw_entry_count": len(entries),  # for skipped-count math in the caller
        "items":       items,
    }


# --------------------------------------------------------------------------
# Progress parsers — best-effort, return None if the line doesn't match
# --------------------------------------------------------------------------

_YT_PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_DEMUCS_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%\|")  # tqdm-style "47%|..."


def _parse_yt_dlp_percent(line: str) -> Optional[float]:
    m = _YT_PCT_RE.search(line)
    if not m: return None
    try: return float(m.group(1))
    except ValueError: return None


def _parse_demucs_percent(line: str) -> Optional[float]:
    m = _DEMUCS_PCT_RE.search(line)
    if not m: return None
    try: return float(m.group(1))
    except ValueError: return None
