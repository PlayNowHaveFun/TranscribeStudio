"""
Transcript analysis via a local Ollama LLM.

>>> LOCAL LLM INTEGRATION POINT <<<
This module sends completed Whisper transcripts to your local qwen2.5-coder:14b
model (or whichever model is configured in project.ollama.model) and writes
an .analysis.json sidecar next to the .txt file.

Analysis sidecar schema:
{
  "model":           "qwen2.5-coder:14b",
  "analyzed_at":     "2026-05-14T...",
  "transcript_path": "/abs/path/transcript.txt",
  "word_count":      1234,
  "analyses":        ["summary", "topics"],
  "summary":         "Two-sentence summary...",
  "topics":          ["Topic A", "Topic B"],
  "error":           null   # or error string on partial failure
}

Yields Event objects matching the engine.py / yt_ingest.py pattern so the
worker can stream progress to the UI via /api/status.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Generator

from .engine import Event
from . import ollama_client


# System prompt given to the LLM for every analysis call.
_SYSTEM = (
    "You are a precise transcript analyst. "
    "You receive a verbatim transcript (possibly multilingual, possibly with "
    "translation artifacts) and produce structured analysis output. "
    "Be concise and factual. Never add content not present in the transcript."
)

# Per-analysis prompt templates. {text} is replaced with the transcript body.
_PROMPTS: dict[str, str] = {
    "summary": (
        "Summarize the following transcript in 2–4 sentences. "
        "Focus on the main subject and key points discussed. "
        "Output only the summary paragraph — no headers, no bullet points.\n\n"
        "Transcript:\n{text}"
    ),
    "topics": (
        "List the main topics discussed in the following transcript. "
        'Output ONLY a JSON array of short strings (max 8 items), e.g. ["Topic A", "Topic B"]. '
        "No other text, no markdown, no explanation.\n\n"
        "Transcript:\n{text}"
    ),
}

# Truncate transcript to this many characters before sending to the LLM.
# Keeps the prompt within a safe context window for 3b–14b models.
# 8000 chars ≈ 1500–2000 tokens depending on language.
MAX_CHARS = 8000


def analyze_transcript(
    transcript_path: str | Path,
    model: str,
    analyses: list[str],
) -> Generator[Event, None, None]:
    """Analyze a Whisper .txt transcript with a local Ollama model.

    >>> LOCAL LLM CALL — sends transcript to qwen2.5-coder:14b (or configured model) <<<

    Args:
        transcript_path: Absolute path to the whisper-produced .txt file.
        model:           Ollama model name, e.g. 'qwen2.5-coder:14b'.
        analyses:        Which analyses to run. Any subset of ["summary", "topics"].
                         Unknown keys are silently ignored.

    Yields:
        Event(phase="analyze_start",    file=name, payload={model, analyses})
        Event(phase="analyze_progress", file=name, payload={step, done, error?})
        Event(phase="analyze_done",     file=name, payload={analysis_path})
        Event(phase="fail",             file=name, payload={reason})

    Side effects:
        Writes <transcript_stem>.analysis.json in the same directory as the .txt.
        Overwrites any existing sidecar.
    """
    transcript_path = Path(transcript_path).resolve()
    name = transcript_path.name

    if not transcript_path.exists():
        yield Event("fail", name, {"reason": f"transcript not found: {transcript_path}"})
        return

    try:
        raw_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        yield Event("fail", name, {"reason": f"could not read transcript: {e}"})
        return

    # Strip chunk-merge markers (lines like "# === chunk 1 ===") whisper writes
    lines = [ln for ln in raw_text.splitlines() if not ln.startswith("# ===")]
    clean_text = "\n".join(lines).strip()
    word_count = len(clean_text.split())

    if not clean_text:
        yield Event("fail", name, {"reason": "transcript is empty"})
        return

    truncated = clean_text[:MAX_CHARS]
    valid_analyses = [a for a in analyses if a in _PROMPTS]
    if not valid_analyses:
        yield Event("fail", name, {"reason": f"no valid analysis types in {analyses!r}"})
        return

    yield Event("analyze_start", name, {"model": model, "analyses": valid_analyses})

    result: dict = {
        "model": model,
        "analyzed_at": datetime.now().isoformat(),
        "transcript_path": str(transcript_path),
        "word_count": word_count,
        "analyses": valid_analyses,
        "summary": None,
        "topics": [],
        "error": None,
    }

    for step in valid_analyses:
        yield Event("analyze_progress", name, {"step": step, "done": False})
        prompt = _PROMPTS[step].format(text=truncated)

        try:
            # >>> LOCAL LLM CALL HERE — blocks until qwen2.5-coder:14b responds <<<
            response = ollama_client.generate(model, prompt, system=_SYSTEM)
        except RuntimeError as e:
            result["error"] = str(e)
            yield Event("analyze_progress", name, {"step": step, "done": True, "error": str(e)})
            break

        if step == "summary":
            result["summary"] = response

        elif step == "topics":
            # Model is asked for a JSON array but sometimes wraps it in ```json fences.
            raw = response.strip()
            if raw.startswith("```"):
                inner_lines = raw.split("\n")[1:]
                raw = "\n".join(l for l in inner_lines if not l.startswith("```")).strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    result["topics"] = [str(t).strip() for t in parsed[:8] if t]
                else:
                    result["topics"] = []
            except json.JSONDecodeError:
                # Fallback: treat each non-empty line as a topic
                result["topics"] = [
                    ln.strip().lstrip("-•* ").strip()
                    for ln in raw.splitlines()
                    if ln.strip()
                ][:8]

        yield Event("analyze_progress", name, {"step": step, "done": True})

    # Write the sidecar next to the .txt
    analysis_path = transcript_path.with_suffix(".analysis.json")
    try:
        analysis_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        yield Event("fail", name, {"reason": f"could not write .analysis.json: {e}"})
        return

    yield Event("analyze_done", name, {"analysis_path": str(analysis_path)})
