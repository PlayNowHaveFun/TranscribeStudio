"""
Ollama HTTP client for Transcribe Studio.

>>> LOCAL LLM INTEGRATION POINT <<<
This module is how the app talks to your locally running Ollama instance
(http://localhost:11434). It uses qwen2.5-coder:14b by default — the
model you have installed. All calls are synchronous and safe to run from
the Flask worker thread.

No new pip dependencies — pure stdlib urllib only.

Ollama API: https://github.com/ollama/llama/blob/main/docs/api.md
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_TIMEOUT = 10    # seconds — used for is_running / list_models
GENERATE_TIMEOUT = 180  # seconds — LLMs can be slow on long transcripts


def is_running() -> bool:
    """Return True if an Ollama server is reachable at OLLAMA_BASE."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=DEFAULT_TIMEOUT) as r:
            return r.status < 500
    except Exception:
        return False


def list_models() -> list[str]:
    """Return sorted list of model names installed in Ollama.

    Example: ['llama3.2:3b', 'qwen2.5-coder:14b', 'qwen2.5-coder:7b']
    Returns [] if Ollama is not running or returns unexpected data.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=DEFAULT_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []) if m.get("name"))
    except Exception:
        return []


def generate(model: str, prompt: str, system: str = "") -> str:
    """Send a non-streaming generate request to Ollama and return the response text.

    >>> LOCAL LLM CALL — qwen2.5-coder:14b (or whichever model is configured) <<<

    Args:
        model:  Ollama model name, e.g. 'qwen2.5-coder:14b'
        prompt: The user-facing prompt (transcript + task instructions)
        system: Optional system prompt prepended to every call.

    Returns:
        The model's response text (stripped).

    Raises:
        RuntimeError on HTTP errors or connection failure — callers log and surface this.
    """
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,   # low temp → more factual, less creative
            "num_predict": 1024,  # cap response length
        },
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/generate",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=GENERATE_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data.get("response", "").strip()
        if not text:
            raise RuntimeError("Ollama returned an empty response")
        return text
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Ollama HTTP {e.code}: {body_text}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama not reachable at {OLLAMA_BASE}: {e.reason}")
