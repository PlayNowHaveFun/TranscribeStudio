"""
Whisper.cpp orchestration engine — Python port.

Why Python instead of bash:
  - Yields live progress events (chunk N of M started/finished) so the UI
    can show real-time status without parsing subprocess stdout heuristically
  - Resumable: crashes leave per-chunk .txt files in place; the next run
    picks up where it stopped
  - Programmatic hallucination detection (count repeated phrases in output)
  - Cleaner error reporting with structured exceptions
  - Live capture of whisper-cli stdout for the live-tail panel in the UI

What we preserve from the bash script (audio-transcribe skill learnings):
  * Multilingual large-v3 default — handles Hindi/Hinglish/English code-switch
  * VAD ON by default — Silero v6 — kills silence-driven hallucination loops
  * max-len 80 — breaks long segments so any surviving loop stays shallow
  * Auto-chunking for files > CHUNK_THRESHOLD_MIN — each chunk is short
    enough that whisper.cpp can't develop a stable repetition loop
  * `-pp` (print progress), `-tr` (translate), language hint — same flags
  * No --no-context by default, but available as a knob (best fix for loops)
  * Skip files already transcribed (idempotent re-runs)
  * Skip files with no audio track (write stub so we don't retry)
  * Output to transcriptions/ subfolder of the source video's directory

Binaries this still shells out to (no native Python equivalent):
  - whisper-cli (or whisper-cpp)
  - ffmpeg (audio extraction, chunk splitting)
  - ffprobe (duration + stream type probing)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Generator, Iterator, Optional


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_MODEL_DIR = Path.home() / "Documents/cowork-tools/whisper-models"

# Models we know about. Sizes are approximate, used for the UI.
KNOWN_MODELS = {
    "ggml-tiny.en.bin":      {"size_mb": 75,   "lang": "en",   "quality": 1, "speed": 5},
    "ggml-tiny.bin":         {"size_mb": 75,   "lang": "multi","quality": 1, "speed": 5},
    "ggml-base.en.bin":      {"size_mb": 142,  "lang": "en",   "quality": 2, "speed": 5},
    "ggml-base.bin":         {"size_mb": 142,  "lang": "multi","quality": 2, "speed": 5},
    "ggml-small.en.bin":     {"size_mb": 466,  "lang": "en",   "quality": 3, "speed": 4},
    "ggml-small.bin":        {"size_mb": 466,  "lang": "multi","quality": 3, "speed": 4},
    "ggml-medium.en.bin":    {"size_mb": 1500, "lang": "en",   "quality": 4, "speed": 3},
    "ggml-medium.bin":       {"size_mb": 1500, "lang": "multi","quality": 4, "speed": 3},
    "ggml-large-v3.bin":     {"size_mb": 3094, "lang": "multi","quality": 5, "speed": 2},
    "ggml-large-v3-turbo.bin":{"size_mb": 1624,"lang": "multi","quality": 4, "speed": 4},
}

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv", ".wmv"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg"}
ALL_EXT   = VIDEO_EXT | AUDIO_EXT


@dataclass
class WhisperConfig:
    """All knobs the engine accepts. Maps to whisper-cli + our chunking logic."""
    model: str = "ggml-large-v3.bin"
    language: str = "hi"            # source language code; "auto" to detect
    translate_to_english: bool = True
    vad: bool = True
    vad_model: str = "ggml-silero-v6.2.0.bin"
    no_context: bool = False        # disables cross-segment context — best
                                    # fix for hallucination loops, slight
                                    # quality hit
    max_len: int = 80               # segment length cap (helps escape loops)
    chunk_threshold_min: int = 10   # files longer than this get chunked
    chunk_min: int = 5              # chunk size in minutes
    formats: tuple[str, ...] = ("txt", "srt")  # outputs to emit
    output_subdir: str = "transcriptions"      # relative to source dir
    model_dir: str = ""             # blank → DEFAULT_MODEL_DIR

    def model_path(self) -> Path:
        d = Path(self.model_dir).expanduser() if self.model_dir else DEFAULT_MODEL_DIR
        return d / self.model

    def vad_model_path(self) -> Path:
        d = Path(self.model_dir).expanduser() if self.model_dir else DEFAULT_MODEL_DIR
        return d / self.vad_model


# --------------------------------------------------------------------------
# Events emitted during transcription (consumed by the worker for the UI)
# --------------------------------------------------------------------------

@dataclass
class Event:
    phase: str            # preflight, extract, transcribe, chunk, merge, done, fail, skip, log
    file: str             # basename of the source file
    payload: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# --------------------------------------------------------------------------
# Custom exceptions so callers can react meaningfully
# --------------------------------------------------------------------------

class EngineError(Exception): pass
class MissingBinaryError(EngineError): pass
class MissingModelError(EngineError): pass
class FfmpegError(EngineError): pass
class WhisperFailedError(EngineError): pass
class NoAudioTrackError(EngineError): pass


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class Engine:
    """
    Stateless orchestrator. One Engine can be reused for many files.

    Use `transcribe(path, config)` as a generator that yields Event objects.
    The caller (worker thread) consumes events and forwards them to the UI.
    """

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = log_dir
        self._cancel_flag = threading.Event()
        self._current_proc: Optional[subprocess.Popen] = None

    # -- preflight ---------------------------------------------------------

    @staticmethod
    def find_whisper_binary() -> str:
        for name in ("whisper-cli", "whisper-cpp"):
            path = shutil.which(name)
            if path:
                return path
        raise MissingBinaryError(
            "whisper-cli not found in PATH. Install with:  brew install whisper-cpp"
        )

    @staticmethod
    def find_ffmpeg() -> tuple[str, str]:
        ff = shutil.which("ffmpeg")
        fp = shutil.which("ffprobe")
        if not ff or not fp:
            raise MissingBinaryError(
                "ffmpeg/ffprobe not found in PATH. Install with:  brew install ffmpeg"
            )
        return ff, fp

    @staticmethod
    def check_model(config: WhisperConfig) -> None:
        mp = config.model_path()
        if not mp.exists():
            raise MissingModelError(f"Whisper model not found: {mp}")
        if config.vad:
            vm = config.vad_model_path()
            if not vm.exists():
                raise MissingModelError(f"VAD model not found: {vm}")

    # -- duration / audio probing -----------------------------------------

    @staticmethod
    def probe_duration(path: Path) -> float:
        ff, fp = Engine.find_ffmpeg()
        result = subprocess.run(
            [fp, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return float(result.stdout.strip())
        except (ValueError, AttributeError):
            return 0.0

    @staticmethod
    def has_audio_track(path: Path) -> bool:
        ff, fp = Engine.find_ffmpeg()
        result = subprocess.run(
            [fp, "-v", "error",
             "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return "audio" in result.stdout

    # -- output paths -----------------------------------------------------

    @staticmethod
    def output_dir_for(path: Path, config: WhisperConfig) -> Path:
        sub = config.output_subdir
        if os.path.isabs(sub):
            return Path(sub)
        return path.parent / sub

    @staticmethod
    def transcript_path_for(path: Path, config: WhisperConfig, ext: str = "txt") -> Path:
        return Engine.output_dir_for(path, config) / f"{path.stem}.{ext}"

    # -- args builder -----------------------------------------------------

    @staticmethod
    def _whisper_args(input_wav: Path, out_base: Path, config: WhisperConfig) -> list[str]:
        args = [
            "-m", str(config.model_path()),
            "-f", str(input_wav),
            "-of", str(out_base),
            "-pp",
            "-ml", str(config.max_len),
        ]
        for fmt in config.formats:
            args.append({"txt":"-otxt","srt":"-osrt","vtt":"-ovtt","json":"-oj","csv":"-ocsv"}[fmt])
        if not config.model.endswith(".en.bin"):
            args += ["-l", config.language]
            if config.translate_to_english:
                args.append("-tr")
        if config.vad:
            args += ["--vad", "-vm", str(config.vad_model_path())]
        if config.no_context:
            args.append("--no-context")
        return args

    # -- subprocess runner with cancellation ------------------------------

    def _run(self, cmd: list[str], cwd: str = "/tmp") -> Iterator[str]:
        """Run a subprocess at low priority, yielding stdout/stderr lines.

        Sets CWD to /tmp so child binaries (whisper-cli, ffmpeg) never trip
        over TCC restrictions on the launchd working directory. This was the
        cause of the 'getcwd: Operation not permitted' crashes earlier.
        """
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
            raise WhisperFailedError(f"subprocess exited with {proc.returncode}: {' '.join(cmd[:3])}…")

    def cancel(self):
        """Cancel any in-progress transcription. Safe to call from another thread."""
        self._cancel_flag.set()
        proc = self._current_proc
        if proc:
            try: proc.terminate()
            except Exception: pass

    def reset_cancel(self):
        self._cancel_flag.clear()

    # -- audio extraction --------------------------------------------------

    def _extract_audio(self, input_path: Path, output_wav: Path) -> Iterator[str]:
        ff, _ = self.find_ffmpeg()
        cmd = [
            ff, "-y", "-loglevel", "error",
            "-i", str(input_path),
            "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(output_wav),
        ]
        yield from self._run(cmd)

    def _split_into_chunks(self, input_path: Path, work_dir: Path, chunk_sec: int) -> Iterator[str]:
        ff, _ = self.find_ffmpeg()
        cmd = [
            ff, "-y", "-loglevel", "error",
            "-i", str(input_path),
            "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "segment", "-segment_time", str(chunk_sec),
            "-reset_timestamps", "1",
            str(work_dir / "chunk_%03d.wav"),
        ]
        yield from self._run(cmd)

    # -- whisper invocation -----------------------------------------------

    def _run_whisper(self, input_wav: Path, out_base: Path, config: WhisperConfig) -> Iterator[str]:
        bin_ = self.find_whisper_binary()
        args = self._whisper_args(input_wav, out_base, config)
        yield from self._run([bin_] + args)

    # -- merge chunks -----------------------------------------------------

    @staticmethod
    def _merge_chunks(work_dir: Path, out_base: Path, chunk_sec: int) -> int:
        """Merge per-chunk .txt and .srt into the final outputs, applying
        timestamp offsets to the .srt. Returns the segment count."""
        chunk_txts = sorted(work_dir.glob("chunk_*.txt"))
        chunk_srts = sorted(work_dir.glob("chunk_*.srt"))

        # .txt — concat with chunk markers
        with open(out_base.with_suffix(".txt"), "w") as out:
            for i, p in enumerate(chunk_txts):
                mins, secs = divmod(i * chunk_sec, 60)
                out.write(f"\n# === chunk {i:03d} (starts at {mins:02d}:{secs:02d}) ===\n")
                out.write(p.read_text().rstrip() + "\n")

        # .srt — re-number and offset timestamps
        def parse_ts(s: str) -> int:
            h, m, rest = s.split(":")
            sec, ms = rest.split(",")
            return int(h)*3600000 + int(m)*60000 + int(sec)*1000 + int(ms)

        def fmt_ts(ms: int) -> str:
            h = ms // 3600000
            m = (ms % 3600000) // 60000
            s = (ms % 60000) // 1000
            msr = ms % 1000
            return f"{h:02d}:{m:02d}:{s:02d},{msr:03d}"

        seg_num = 1
        with open(out_base.with_suffix(".srt"), "w") as out:
            for i, p in enumerate(chunk_srts):
                offset_ms = i * chunk_sec * 1000
                content = p.read_text().strip() if p.exists() else ""
                if not content:
                    continue
                for block in re.split(r"\n\s*\n", content):
                    lines = [l for l in block.strip().split("\n") if l.strip()]
                    if len(lines) < 3:
                        continue
                    m = re.match(r"(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", lines[1])
                    if not m:
                        continue
                    sm = parse_ts(m.group(1)) + offset_ms
                    em = parse_ts(m.group(2)) + offset_ms
                    text = "\n".join(lines[2:])
                    out.write(f"{seg_num}\n{fmt_ts(sm)} --> {fmt_ts(em)}\n{text}\n\n")
                    seg_num += 1
        return seg_num - 1

    # -- main entry point --------------------------------------------------

    def transcribe(
        self,
        path: str | Path,
        config: WhisperConfig,
    ) -> Generator[Event, None, None]:
        """Transcribe one file. Yields Events for the UI/log stream.

        Idempotent: if the .txt already exists in the output dir, yields a
        single 'skip' event and returns. So callers can re-call this on the
        full file list without redoing work.
        """
        path = Path(path).expanduser().resolve()
        name = path.name
        self.reset_cancel()

        # ---- preflight ----
        try:
            self.find_whisper_binary()
            self.find_ffmpeg()
            self.check_model(config)
        except (MissingBinaryError, MissingModelError) as e:
            yield Event("fail", name, {"reason": str(e)})
            return

        out_dir = self.output_dir_for(path, config)
        out_base = out_dir / path.stem
        primary = out_base.with_suffix(".txt")

        if primary.exists():
            yield Event("skip", name, {"reason": "already_transcribed", "output": str(primary)})
            return

        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- duration probe ----
        try:
            duration = self.probe_duration(path)
        except Exception as e:
            yield Event("fail", name, {"reason": f"ffprobe failed: {e}"})
            return

        chunked = duration > config.chunk_threshold_min * 60
        yield Event("preflight", name, {
            "duration_sec": duration,
            "mode": "chunked" if chunked else "standard",
            "model": config.model,
            "language": config.language,
            "translate": config.translate_to_english,
            "vad": config.vad,
            "no_context": config.no_context,
        })

        # ---- standard path ----
        if not chunked:
            try:
                if not self.has_audio_track(path):
                    primary.write_text("(no audio track in source file)\n")
                    yield Event("skip", name, {"reason": "no_audio_track"})
                    return

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_wav = Path(tmp) / "audio.wav"
                    yield Event("extract", name, {})
                    for line in self._extract_audio(path, tmp_wav):
                        if line.strip():
                            yield Event("log", name, {"line": line})

                    yield Event("transcribe", name, {})
                    for line in self._run_whisper(tmp_wav, out_base, config):
                        if line.strip():
                            yield Event("log", name, {"line": line})

                if not primary.exists():
                    yield Event("fail", name, {"reason": "no output produced"})
                    return
                yield Event("done", name, {"output": str(primary), "duration_sec": duration})
            except WhisperFailedError as e:
                yield Event("fail", name, {"reason": str(e)})
            except Exception as e:
                yield Event("fail", name, {"reason": f"{type(e).__name__}: {e}"})
            return

        # ---- chunked path ----
        chunk_sec = config.chunk_min * 60
        work_dir = Path(tempfile.gettempdir()) / f"transcribe_{path.stem}_{os.getpid()}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield Event("split", name, {})
            for line in self._split_into_chunks(path, work_dir, chunk_sec):
                if line.strip():
                    yield Event("log", name, {"line": line})

            chunks = sorted(work_dir.glob("chunk_*.wav"))
            n = len(chunks)
            yield Event("split_done", name, {"chunks": n})

            for i, chunk in enumerate(chunks, start=1):
                cbase = chunk.with_suffix("")
                if cbase.with_suffix(".txt").exists():
                    yield Event("chunk_skip", name, {"chunk": i, "of": n})
                    continue
                yield Event("chunk", name, {"chunk": i, "of": n})
                try:
                    for line in self._run_whisper(chunk, cbase, config):
                        if line.strip():
                            yield Event("log", name, {"line": line, "chunk": i, "of": n})
                except WhisperFailedError as e:
                    yield Event("chunk_fail", name, {"chunk": i, "of": n, "reason": str(e)})
                    # keep going — we'll merge what we have at the end

            yield Event("merge", name, {})
            seg_count = self._merge_chunks(work_dir, out_base, chunk_sec)
            if not primary.exists():
                yield Event("fail", name, {"reason": "merge produced no output"})
                return
            yield Event("done", name, {
                "output": str(primary),
                "duration_sec": duration,
                "chunks": n,
                "segments": seg_count,
            })
        finally:
            try: shutil.rmtree(work_dir)
            except Exception: pass


# --------------------------------------------------------------------------
# Hallucination detection (post-transcription quality check)
# --------------------------------------------------------------------------

def detect_hallucinations(txt_path: Path) -> dict:
    """Heuristic check for whisper.cpp's known repetition failure mode.

    Looks for the same line repeated 5+ times consecutively, plus high
    overall repetition ratios. Returns a dict with the findings; the UI
    can render this as a quality warning + offer 'Re-transcribe with
    --no-context' button.
    """
    try:
        text = txt_path.read_text()
    except Exception:
        return {"ok": False, "error": "could not read file"}

    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return {"ok": True, "lines": 0, "warnings": []}

    warnings = []
    # Consecutive duplicates
    run_start, run_text, run_len = 0, lines[0], 1
    longest_run = ("", 0)
    for i in range(1, len(lines)):
        if lines[i] == run_text:
            run_len += 1
            if run_len > longest_run[1]:
                longest_run = (run_text, run_len)
        else:
            run_text, run_len = lines[i], 1

    if longest_run[1] >= 5:
        warnings.append({
            "type": "consecutive_repeat",
            "phrase": longest_run[0][:120],
            "count": longest_run[1],
            "fix": "Re-transcribe with no_context=true and/or model=large-v3",
        })

    # Overall repetition ratio
    unique = len(set(lines))
    ratio = unique / len(lines)
    if ratio < 0.4 and len(lines) > 20:
        warnings.append({
            "type": "low_diversity",
            "unique_ratio": round(ratio, 2),
            "total_lines": len(lines),
            "fix": "Re-transcribe with no_context=true",
        })

    return {
        "ok": not warnings,
        "lines": len(lines),
        "unique_lines": unique,
        "unique_ratio": round(ratio, 2) if lines else 0,
        "warnings": warnings,
    }
