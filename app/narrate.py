"""
Narrative layer — turns a whisper transcript into creative prose via Claude Opus 4.7.

Coexists with `app/analyzer.py` (local Ollama: summary + topics):
  - analyzer.py  → writes <stem>.analysis.json  (factual: summary, topics)
  - narrate.py   → writes <stem>.narrative.scaffold.json   (literary: themes, beats, characters)
                 + <stem>.narrative.<style>.md            (creative prose)

The two run on different triggers (Ollama is auto on transcript completion if
project.ollama.enabled; narrate is always manual via the UI button) and serve
different purposes (analysis is the *what*, narrative is the *story*).

Two-stage Claude Opus 4.7 pipeline:
  1. analyze_transcript(text) → {themes, characters, story_beats, emotional_arc, quotes}
  2. generate_narrative(text, scaffold, style) → Markdown prose

Model choice (claude-opus-4-7) was researched: Opus 4.7 currently tops the
Mazur Writing benchmark, Arena instruction-following, and EQ Creative for
narrative quality. Specialized creative LLMs (Sudowrite Muse, NovelAI Kayra)
beat general models on novelist workflows, not on transcript→narrative quality,
and lack clean APIs.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional


MODEL = "claude-opus-4-7"
MAX_TOKENS_ANALYZE = 4096
MAX_TOKENS_NARRATE = 8192

# Single-pass character ceiling. Opus 4.7's 200k-token window comfortably fits
# ~600k chars; we cap lower so the model has headroom for output + reasoning.
# Above this we'd need map-reduce (analyze chunks, then synthesize) — deferred
# until a real transcript actually trips this limit.
SINGLE_PASS_MAX_CHARS = 400_000


STYLES = {
    "story": (
        "Transform the transcript into a literary short-story-form narrative. "
        "Preserve the speaker's voice and the truth of what happened. You may "
        "compress, reorder for emotional clarity, and find the through-line. "
        "Do not invent events, names, or dialogue beats that aren't in the source."
    ),
    "reflective_essay": (
        "Rewrite the transcript as a first-person reflective essay focused on "
        "insight and meaning. Preserve the speaker's voice. You may compress and "
        "reorder for clarity. Do not invent events or claims that aren't in the source."
    ),
    "scene_breakdown": (
        "Reshape the transcript into a scene-by-scene structure with brief stage "
        "directions, character beats, and dialogue. Stay faithful to what was "
        "actually said; treat the transcript as the source script."
    ),
    "summary_narrative": (
        "Write a tight prose summary (~400–700 words) that reads like a story, "
        "not a bullet list. Hit the key moments, the emotional shape, and the "
        "resolution. Preserve voice."
    ),
}


class NarrateError(Exception):
    """Raised for any failure during scaffold or narrative generation."""


# --------------------------------------------------------------------------
# Sidecar path helpers (kept separate so callers + tests can target them)
# --------------------------------------------------------------------------

def scaffold_path_for(transcript_path: Path) -> Path:
    """Path to the literary scaffold sidecar (themes/beats/characters)."""
    return Path(transcript_path).with_suffix(".narrative.scaffold.json")


def narrative_path_for(transcript_path: Path, style: str) -> Path:
    """Path to the creative narrative Markdown for a given style."""
    return Path(transcript_path).with_suffix(f".narrative.{style}.md")


# --------------------------------------------------------------------------
# Anthropic client (lazy — so importing this module doesn't require the key)
# --------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise NarrateError(
            "anthropic SDK not installed. Run `pip install -r requirements.txt`."
        ) from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise NarrateError(
            "ANTHROPIC_API_KEY not set. Export it before generating narratives."
        )
    _client = Anthropic()
    return _client


# --------------------------------------------------------------------------
# Stage 1 — literary scaffold
# --------------------------------------------------------------------------

_ANALYZE_SYSTEM = """You are a narrative analyst. You read a transcript of speech (often messy, sometimes translated, often unfiltered) and surface the story inside it.

Return ONLY a JSON object with these keys — no preamble, no markdown fence:

{
  "themes": [3 to 7 short strings describing what this transcript is really about],
  "characters": [{"name": "...", "role": "speaker|listener|mentioned", "description": "one short sentence"}],
  "story_beats": [{"moment": "...", "significance": "..."}, ... in chronological order],
  "emotional_arc": "3 to 5 sentence description of how the emotional energy moves from start to finish",
  "notable_quotes": [{"quote": "exact words from transcript", "context": "what was happening", "why_resonant": "..."}],
  "summary_one_line": "single sentence that captures the heart of it"
}

Be specific. Surface what is actually in the transcript — do not invent characters or events. Quotes must be exact strings from the source. If the transcript is too short or empty, return the schema with empty arrays and explain in summary_one_line."""


def analyze_transcript(text: str, language_hint: str = "en") -> dict:
    """Stage 1: extract structured literary scaffold from a transcript.

    This is the *narrative* scaffold (themes, beats, characters, arc) used to
    guide creative prose generation — separate from Ollama's factual analysis
    (summary + topics) in app/analyzer.py.

    Returns a dict matching the schema in _ANALYZE_SYSTEM. Raises NarrateError
    on API failure or unparseable JSON output.
    """
    if not text or not text.strip():
        raise NarrateError("transcript is empty — nothing to analyze")
    if len(text) > SINGLE_PASS_MAX_CHARS:
        raise NarrateError(
            f"transcript is {len(text):,} chars (>{SINGLE_PASS_MAX_CHARS:,}); "
            "single-pass scaffolding would exceed model context. "
            "Map-reduce mode is not implemented yet."
        )

    client = _get_client()
    user_msg = f"Language hint: {language_hint}\n\nTranscript:\n\n{text}"
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_ANALYZE,
            system=_ANALYZE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        raise NarrateError(f"Anthropic API error during scaffold: {e}") from e

    raw = _extract_text(resp)
    return _parse_json_strict(raw)


# --------------------------------------------------------------------------
# Stage 2 — creative narrative
# --------------------------------------------------------------------------

_NARRATE_SYSTEM_TEMPLATE = """You are a literary writer working from a transcript and a structural analysis of it.

Your task: {style_instruction}

Constraints:
- Stay true to the source — do not invent events, claims, names, or dialogue beats that aren't grounded in the transcript.
- Preserve the speaker's voice and rhythm. If they're plain-spoken, write plain. If they're poetic, write poetic.
- Output Markdown. Use headings, paragraph breaks, and emphasis where they serve the reading.
- No preamble like "Here is the narrative:" — just the piece itself.

Use the scaffold as your structural guide (themes, beats, arc, quotes), but the prose should read naturally — not as a recap of the scaffold."""


def generate_narrative(
    text: str,
    scaffold: dict,
    style: str = "story",
) -> str:
    """Stage 2: produce creative narrative Markdown.

    `style` must be one of STYLES. Raises NarrateError on bad style or API failure.
    """
    if style not in STYLES:
        raise NarrateError(
            f"unknown style {style!r}; valid: {sorted(STYLES)}"
        )
    if not text or not text.strip():
        raise NarrateError("transcript is empty — nothing to narrate")
    if len(text) > SINGLE_PASS_MAX_CHARS:
        raise NarrateError(
            f"transcript is {len(text):,} chars (>{SINGLE_PASS_MAX_CHARS:,}); "
            "single-pass narration would exceed model context."
        )

    client = _get_client()
    system = _NARRATE_SYSTEM_TEMPLATE.format(style_instruction=STYLES[style])
    user_msg = (
        f"## Scaffold (structural guide)\n\n"
        f"{json.dumps(scaffold, indent=2, ensure_ascii=False)}\n\n"
        f"## Transcript (source of truth)\n\n"
        f"{text}"
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_NARRATE,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        raise NarrateError(f"Anthropic API error during narration: {e}") from e

    return _extract_text(resp).strip()


# --------------------------------------------------------------------------
# Orchestrator — reads transcript, runs both stages, writes sidecars
# --------------------------------------------------------------------------

def narrate(
    transcript_path: Path,
    style: str = "story",
    language_hint: str = "en",
    force: bool = False,
) -> dict:
    """Read a transcript .txt and produce both scaffold + narrative.

    Sidecar paths (next to the .txt):
        <stem>.narrative.scaffold.json
        <stem>.narrative.<style>.md

    If both sidecars exist and `force` is False, returns cached content
    without calling the API.

    Returns: {scaffold_path, narrative_path, scaffold, narrative, cached}
    """
    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        raise NarrateError(f"transcript not found: {transcript_path}")

    scaffold_path = scaffold_path_for(transcript_path)
    narrative_path = narrative_path_for(transcript_path, style)

    if not force and scaffold_path.exists() and narrative_path.exists():
        try:
            scaffold = json.loads(scaffold_path.read_text())
            narrative = narrative_path.read_text()
            return {
                "scaffold_path": str(scaffold_path),
                "narrative_path": str(narrative_path),
                "scaffold": scaffold,
                "narrative": narrative,
                "cached": True,
            }
        except Exception:
            pass  # fall through and regenerate

    text = transcript_path.read_text()

    # Re-use the scaffold cache across styles — scaffold doesn't depend on style.
    if not force and scaffold_path.exists():
        try:
            scaffold = json.loads(scaffold_path.read_text())
        except Exception:
            scaffold = analyze_transcript(text, language_hint=language_hint)
            _write_atomic(scaffold_path, json.dumps(scaffold, indent=2, ensure_ascii=False))
    else:
        scaffold = analyze_transcript(text, language_hint=language_hint)
        _write_atomic(scaffold_path, json.dumps(scaffold, indent=2, ensure_ascii=False))

    narrative = generate_narrative(text, scaffold, style=style)
    _write_atomic(narrative_path, narrative)

    return {
        "scaffold_path": str(scaffold_path),
        "narrative_path": str(narrative_path),
        "scaffold": scaffold,
        "narrative": narrative,
        "cached": False,
    }


def read_cached(transcript_path: Path, style: str = "story") -> Optional[dict]:
    """Return cached scaffold+narrative if both sidecars exist, else None.

    Used by the GET endpoint so the UI can fetch without triggering generation.
    """
    transcript_path = Path(transcript_path)
    scaffold_path = scaffold_path_for(transcript_path)
    narrative_path = narrative_path_for(transcript_path, style)
    if not (scaffold_path.exists() and narrative_path.exists()):
        return None
    try:
        return {
            "scaffold_path": str(scaffold_path),
            "narrative_path": str(narrative_path),
            "scaffold": json.loads(scaffold_path.read_text()),
            "narrative": narrative_path.read_text(),
            "cached": True,
        }
    except Exception:
        return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _extract_text(resp) -> str:
    """Pull the text out of an Anthropic messages.create() response.

    The SDK returns a Message with `content` as a list of blocks; for our
    prompts we always get a single text block.
    """
    if not getattr(resp, "content", None):
        raise NarrateError("Anthropic response had no content")
    block = resp.content[0]
    text = getattr(block, "text", None)
    if not text:
        raise NarrateError(f"Anthropic response block was not text: {block!r}")
    return text


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _parse_json_strict(raw: str) -> dict:
    """Parse JSON from an LLM response, tolerating optional ```json fences.

    Raises NarrateError with a useful snippet if parsing fails.
    """
    s = raw.strip()
    m = _JSON_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        snippet = s[:300] + ("…" if len(s) > 300 else "")
        raise NarrateError(
            f"could not parse JSON from scaffold response ({e.msg}). "
            f"Got: {snippet!r}"
        ) from e


def _write_atomic(path: Path, content: str) -> None:
    """Write a sidecar file atomically (so partial writes never linger)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)
