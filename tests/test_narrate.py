"""
Tests for app/narrate.py.

- Unit tests stub out the Anthropic SDK by monkey-patching the lazy client
  factory in narrate. This keeps tests offline and free.
- One live smoke test runs only when ANTHROPIC_API_KEY is set in the env.
  It validates the real Opus 4.7 pipeline end-to-end against a tiny fixture.

Run:
    pytest tests/test_narrate.py -v
    ANTHROPIC_API_KEY=sk-... pytest tests/test_narrate.py -v -k live
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the studio package importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import narrate  # noqa: E402


FIXTURE_TRANSCRIPT = """Today I went to the lake. The water was glassy and cold.
I sat on the dock for almost an hour. I thought about my mother.
I wanted to call her but I didn't. I wrote a note instead and folded it.
Walking back, I noticed a small dog following me. It seemed lost.
I took it home and gave it water. I don't know what I'm doing anymore.
But I feel a little less alone tonight."""

FAKE_SCAFFOLD = {
    "themes": ["solitude", "memory", "small acts of care"],
    "characters": [
        {"name": "narrator", "role": "speaker", "description": "introspective, lonely"},
        {"name": "mother", "role": "mentioned", "description": "unreachable"},
        {"name": "lost dog", "role": "mentioned", "description": "unexpected companion"},
    ],
    "story_beats": [
        {"moment": "sits by the lake", "significance": "quiet self-confrontation"},
        {"moment": "doesn't call mother", "significance": "the held-back gesture"},
        {"moment": "takes in the dog", "significance": "tenderness redirected"},
    ],
    "emotional_arc": "starts in stillness, slides into longing, ends in tentative warmth.",
    "notable_quotes": [
        {"quote": "I don't know what I'm doing anymore.", "context": "after taking the dog home", "why_resonant": "names the drift the whole piece is about"},
    ],
    "summary_one_line": "A solitary day at the lake ends in unexpected company.",
}

FAKE_NARRATIVE = "## At the Lake\n\nThe water was glassy that morning. I sat for a long time...\n\nWalking home, the dog appeared."


# --------------------------------------------------------------------------
# Stub helpers
# --------------------------------------------------------------------------

class _FakeClient:
    """Minimal stand-in for anthropic.Anthropic — records calls + returns canned content."""

    def __init__(self, responses):
        # responses: list of text strings, returned in order
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._responses.pop(0)
        block = SimpleNamespace(text=text)
        return SimpleNamespace(content=[block])


@pytest.fixture(autouse=True)
def reset_client():
    """Make sure each test starts with no cached client."""
    narrate._client = None
    yield
    narrate._client = None


@pytest.fixture
def fake_client(monkeypatch):
    """Inject a controllable stub client. Pass in the responses you want returned."""
    def _install(responses):
        client = _FakeClient(responses)
        monkeypatch.setattr(narrate, "_get_client", lambda: client)
        return client
    return _install


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------

def test_analyze_transcript_parses_clean_json(fake_client):
    fake_client([json.dumps(FAKE_SCAFFOLD)])
    result = narrate.analyze_transcript(FIXTURE_TRANSCRIPT)
    assert result["themes"] == FAKE_SCAFFOLD["themes"]
    assert result["summary_one_line"] == FAKE_SCAFFOLD["summary_one_line"]


def test_analyze_transcript_tolerates_json_fence(fake_client):
    fenced = "```json\n" + json.dumps(FAKE_SCAFFOLD) + "\n```"
    fake_client([fenced])
    result = narrate.analyze_transcript(FIXTURE_TRANSCRIPT)
    assert result["themes"] == FAKE_SCAFFOLD["themes"]


def test_analyze_transcript_raises_on_garbage_output(fake_client):
    fake_client(["this isn't JSON at all"])
    with pytest.raises(narrate.NarrateError, match="parse JSON"):
        narrate.analyze_transcript(FIXTURE_TRANSCRIPT)


def test_analyze_transcript_rejects_empty():
    with pytest.raises(narrate.NarrateError, match="empty"):
        narrate.analyze_transcript("   ")


def test_generate_narrative_returns_text(fake_client):
    client = fake_client([FAKE_NARRATIVE])
    result = narrate.generate_narrative(FIXTURE_TRANSCRIPT, FAKE_SCAFFOLD, style="story")
    assert result == FAKE_NARRATIVE.strip()
    sys_prompt = client.calls[0]["system"]
    assert "literary short-story-form" in sys_prompt


def test_generate_narrative_rejects_unknown_style(fake_client):
    fake_client([])
    with pytest.raises(narrate.NarrateError, match="unknown style"):
        narrate.generate_narrative(FIXTURE_TRANSCRIPT, FAKE_SCAFFOLD, style="haiku")


def test_narrate_orchestrator_writes_sidecars_and_returns_results(fake_client, tmp_path):
    fake_client([json.dumps(FAKE_SCAFFOLD), FAKE_NARRATIVE])
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)

    result = narrate.narrate(tx, style="story")

    assert result["cached"] is False
    assert result["scaffold"]["themes"] == FAKE_SCAFFOLD["themes"]
    assert FAKE_NARRATIVE.strip() in result["narrative"]

    scaffold_file = narrate.scaffold_path_for(tx)
    narrative_file = narrate.narrative_path_for(tx, "story")
    assert scaffold_file.exists()
    assert narrative_file.exists()
    assert json.loads(scaffold_file.read_text())["themes"] == FAKE_SCAFFOLD["themes"]


def test_sidecar_filenames_do_not_collide_with_ollama_analyzer(tmp_path):
    """Regression: the canonical install runs Ollama's analyzer.py which writes
    `<stem>.analysis.json`. Narrate must NOT touch that filename."""
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    scaffold = narrate.scaffold_path_for(tx)
    narrative = narrate.narrative_path_for(tx, "story")
    assert scaffold.name == "session.narrative.scaffold.json"
    assert narrative.name == "session.narrative.story.md"
    # Critical: must not be the Ollama analyzer's filename
    assert scaffold.name != "session.analysis.json"


def test_narrate_uses_cache_on_second_call(fake_client, tmp_path):
    client = fake_client([json.dumps(FAKE_SCAFFOLD), FAKE_NARRATIVE])
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    narrate.narrate(tx, style="story")
    first_call_count = len(client.calls)
    assert first_call_count == 2

    # Second call: should not hit the API at all — cache returns immediately.
    result2 = narrate.narrate(tx, style="story")
    assert result2["cached"] is True
    assert len(client.calls) == first_call_count


def test_narrate_force_regenerates(fake_client, tmp_path):
    client = fake_client([
        json.dumps(FAKE_SCAFFOLD),
        FAKE_NARRATIVE,
        json.dumps({**FAKE_SCAFFOLD, "summary_one_line": "rewritten."}),
        "## Rewritten\n\nNew prose.",
    ])
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    narrate.narrate(tx, style="story")
    assert len(client.calls) == 2

    result = narrate.narrate(tx, style="story", force=True)
    assert result["cached"] is False
    assert result["scaffold"]["summary_one_line"] == "rewritten."
    assert "New prose" in result["narrative"]
    assert len(client.calls) == 4


def test_narrate_reuses_scaffold_across_styles(fake_client, tmp_path):
    """Same transcript, different style → scaffold is cached, only narrative regenerates."""
    client = fake_client([
        json.dumps(FAKE_SCAFFOLD),
        FAKE_NARRATIVE,
        "## Essay form\n\nDifferent prose.",
    ])
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    narrate.narrate(tx, style="story")
    narrate.narrate(tx, style="reflective_essay")

    # 2 calls for the first run, +1 narrative call for the second style.
    assert len(client.calls) == 3
    assert narrate.narrative_path_for(tx, "story").exists()
    assert narrate.narrative_path_for(tx, "reflective_essay").exists()


def test_read_cached_returns_none_when_absent(tmp_path):
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    assert narrate.read_cached(tx) is None


def test_read_cached_returns_content_when_present(tmp_path):
    tx = tmp_path / "session.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    narrate.scaffold_path_for(tx).write_text(json.dumps(FAKE_SCAFFOLD))
    narrate.narrative_path_for(tx, "story").write_text(FAKE_NARRATIVE)
    result = narrate.read_cached(tx, style="story")
    assert result is not None
    assert result["scaffold"]["themes"] == FAKE_SCAFFOLD["themes"]
    assert result["narrative"] == FAKE_NARRATIVE


def test_oversized_transcript_is_rejected(fake_client):
    fake_client([])
    big = "x" * (narrate.SINGLE_PASS_MAX_CHARS + 1)
    with pytest.raises(narrate.NarrateError, match="exceed model context"):
        narrate.analyze_transcript(big)


# --------------------------------------------------------------------------
# Live smoke test (gated on real API key)
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; skipping live smoke test",
)
def test_live_smoke_end_to_end(tmp_path):
    """Real API call — runs only when ANTHROPIC_API_KEY is exported."""
    tx = tmp_path / "smoke.txt"
    tx.write_text(FIXTURE_TRANSCRIPT)
    result = narrate.narrate(tx, style="summary_narrative")
    assert result["scaffold"].get("themes"), "expected non-empty themes from real model"
    assert len(result["narrative"]) > 50, "expected substantial narrative text"
