"""
Transcribe Studio — Flask backend.

Run with:
    python -m app.main          (from STUDIO_ROOT)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, render_template, send_file, abort

from . import conditions
from . import narrate as narrate_mod
from .engine import (
    Engine, WhisperConfig, KNOWN_MODELS, ALL_EXT, detect_hallucinations,
)
from .projects import Project, Registry, slugify, STUDIO_ROOT
from .scanner import scan_project, order_files, annotate_with_state, next_pending
from .transcriber import Worker


PORT = 5180
HOST = "127.0.0.1"

LOG_PATH = STUDIO_ROOT / "logs" / "studio.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _bootstrap_default_projects(registry: Registry) -> None:
    """First-launch: if no projects exist, create Samvaad as project #1
    with the proven Hindi/large-v3/VAD config and pre-mark already-existing
    transcripts as completed (so progress isn't lost from the bash setup)."""
    if registry.all():
        return
    samvaad_internal = Path.home() / "Documents/Art/Samvaad"
    samvaad_external = Path("/Volumes/Offline_Database/Samvaad")
    if not samvaad_internal.exists():
        return
    folders = [str(samvaad_internal)]
    required_volumes = []
    if samvaad_external.parent.exists() or samvaad_external.exists():
        folders.append(str(samvaad_external))
        required_volumes.append("/Volumes/Offline_Database")
    proj = Project(
        id="samvaad",
        name="Samvaad — Therapy Sessions",
        folders=folders,
        config=WhisperConfig(
            model="ggml-large-v3.bin",
            language="hi",
            translate_to_english=True,
            vad=True,
            no_context=False,
            chunk_threshold_min=10,
            chunk_min=5,
        ),
        auto_run=True,
        require_ac_power=True,
        required_volumes=required_volumes,
        exclude_patterns=["test_clip_*", ".*"],
        ordering="newest_first",
        notes="Imported automatically. Edit folders/settings anytime.",
    )
    registry.add(proj)


def create_app() -> tuple[Flask, Registry, Worker]:
    app = Flask(
        __name__,
        template_folder=str(STUDIO_ROOT / "app" / "templates"),
        static_folder=str(STUDIO_ROOT / "app" / "static"),
    )
    registry = Registry()
    _bootstrap_default_projects(registry)
    worker = Worker(registry, LOG_PATH)

    # ----- view -----
    @app.route("/")
    def index():
        return render_template("index.html")

    # ----- system status -----
    @app.route("/api/status")
    def api_status():
        return jsonify({
            "worker": worker.status(),
            "projects": [_summary(p, registry) for p in registry.all()],
            "system": {
                "ac_power": conditions.on_ac_power(),
                "battery_percent": conditions.battery_percent(),
                "models_dir": str(Path.home() / "Documents/cowork-tools/whisper-models"),
                "host": HOST,
                "port": PORT,
            },
        })

    # ----- worker controls -----
    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        worker.pause()
        return jsonify({"ok": True, "paused": True})

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        worker.resume()
        return jsonify({"ok": True, "paused": False})

    @app.route("/api/wake", methods=["POST"])
    def api_wake():
        worker.wake()
        return jsonify({"ok": True})

    # ----- projects CRUD -----
    @app.route("/api/projects", methods=["GET"])
    def api_projects_list():
        return jsonify([_full(p, registry) for p in registry.all()])

    @app.route("/api/projects", methods=["POST"])
    def api_projects_create():
        body = request.get_json(force=True)
        pid = body.get("id") or slugify(body["name"])
        cfg_dict = body.get("config", {})
        cfg_dict.setdefault("model", "ggml-large-v3.bin")
        cfg_dict.setdefault("language", "auto")
        cfg_dict.setdefault("translate_to_english", False)
        cfg_dict["formats"] = tuple(cfg_dict.get("formats", ("txt", "srt")))
        from .projects import OllamaConfig
        ollama_d = body.get("ollama", {})
        ollama_cfg = OllamaConfig(
            enabled=ollama_d.get("enabled", False),
            model=ollama_d.get("model", "qwen2.5-coder:14b"),
            analyses=ollama_d.get("analyses", ["summary", "topics"]),
            base_url=ollama_d.get("base_url", "http://localhost:11434"),
        )
        proj = Project(
            id=pid,
            name=body["name"],
            folders=body.get("folders", []),
            config=WhisperConfig(**cfg_dict),
            auto_run=body.get("auto_run", True),
            require_ac_power=body.get("require_ac_power", True),
            required_volumes=body.get("required_volumes", []),
            exclude_patterns=body.get("exclude_patterns", []),
            ordering=body.get("ordering", "newest_first"),
            notes=body.get("notes", ""),
            youtube_enabled=body.get("youtube_enabled", False),
            youtube_default_mode=body.get("youtube_default_mode", "speech"),
            youtube_subdir=body.get("youtube_subdir", "youtube"),
            music_keep_vocals=body.get("music_keep_vocals", True),
            music_force_no_context=body.get("music_force_no_context", True),
            music_demucs_segment=body.get("music_demucs_segment", 0),
            ollama=ollama_cfg,
        )
        registry.add(proj)
        worker.wake()
        return jsonify(_full(proj, registry))

    @app.route("/api/projects/<pid>", methods=["GET"])
    def api_projects_get(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        return jsonify(_full(p, registry))

    @app.route("/api/projects/<pid>", methods=["PATCH"])
    def api_projects_update(pid):
        body = request.get_json(force=True)
        p = registry.update(pid, **body)
        if not p:
            abort(404)
        worker.wake()
        return jsonify(_full(p, registry))

    @app.route("/api/projects/<pid>", methods=["DELETE"])
    def api_projects_delete(pid):
        ok = registry.delete(pid)
        if not ok:
            abort(404)
        return jsonify({"ok": True})

    # ----- per-project files -----
    @app.route("/api/projects/<pid>/files")
    def api_files(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        rows = annotate_with_state(scan_project(p, state), state)
        rows = order_files(rows, p.ordering)
        return jsonify(rows)

    @app.route("/api/projects/<pid>/transcript")
    def api_transcript(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        path = request.args.get("path", "")
        if not path:
            abort(400, "missing ?path=")
        src = Path(path)
        tx = Engine.transcript_path_for(src, p.config, "txt")
        srt = Engine.transcript_path_for(src, p.config, "srt")
        out = {"path": path}
        if tx.exists():
            out["txt"] = tx.read_text()
            out["txt_path"] = str(tx)
            out["quality"] = detect_hallucinations(tx)
            # Include Ollama analysis sidecar if it exists
            analysis_path = tx.with_suffix(".analysis.json")
            if analysis_path.exists():
                try:
                    out["analysis"] = json.loads(analysis_path.read_text())
                except Exception:
                    out["analysis"] = None
            else:
                out["analysis"] = None
        else:
            out["txt"] = None
            out["analysis"] = None
        if srt.exists():
            out["srt_path"] = str(srt)
        return jsonify(out)

    # ----- Ollama / local LLM routes -----

    @app.route("/api/ollama/models")
    def api_ollama_models():
        """Return available Ollama models and whether the server is reachable.

        >>> LOCAL LLM QUERY — asks qwen2.5-coder:14b's Ollama server for installed models <<<

        Response: {"running": bool, "models": ["qwen2.5-coder:14b", ...]}
        """
        from .ollama_client import is_running, list_models
        running = is_running()
        models = list_models() if running else []
        return jsonify({"running": running, "models": models})

    @app.route("/api/projects/<pid>/analyze", methods=["POST"])
    def api_analyze(pid):
        """Trigger on-demand Ollama analysis for a single transcript.

        >>> LOCAL LLM CALL — sends transcript to qwen2.5-coder:14b synchronously <<<

        Body: {"path": "/abs/path/to/source_file.mp3"}
        Finds the .txt, runs analyze_transcript(), returns {"ok": true}.
        """
        p = registry.get(pid)
        if not p:
            abort(404)
        body = request.get_json(force=True) or {}
        path = body.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        src = Path(path)
        tx = Engine.transcript_path_for(src, p.config, "txt")
        if not tx.exists():
            return jsonify({"error": "transcript not found", "looked_for": str(tx)}), 404
        model = p.ollama.model or "qwen2.5-coder:14b"
        analyses = p.ollama.analyses or ["summary", "topics"]
        from .analyzer import analyze_transcript
        events = list(analyze_transcript(tx, model, analyses))
        failed = next((e for e in events if e.phase == "fail"), None)
        if failed:
            return jsonify({"error": failed.payload.get("reason", "analysis failed")}), 500
        return jsonify({"ok": True})

    # ----- creative narrative (Claude Opus 4.7) -----
    # Coexists with Ollama analysis above: analyzer.py does fast/local factual
    # analysis (summary + topics) auto-on-completion; narrate is on-demand
    # literary rewrite, gated on ANTHROPIC_API_KEY. Separate sidecars
    # (<stem>.narrative.scaffold.json + <stem>.narrative.<style>.md) so the
    # two systems never collide on disk.

    @app.route("/api/projects/<pid>/narrative", methods=["GET"])
    def api_narrative_get(pid):
        """Return cached scaffold+narrative for a transcript, or 404 if absent.

        Query: ?path=<source>&style=story (style defaults to "story")
        """
        p = registry.get(pid)
        if not p:
            abort(404)
        path = request.args.get("path", "")
        style = request.args.get("style", "story")
        if not path:
            abort(400, "missing ?path=")
        tx = Engine.transcript_path_for(Path(path), p.config, "txt")
        if not tx.exists():
            abort(404, "transcript not generated yet")
        cached = narrate_mod.read_cached(tx, style=style)
        if not cached:
            abort(404, "no narrative cached yet — POST to /narrate to generate")
        return jsonify(cached)

    @app.route("/api/projects/<pid>/narrate", methods=["POST"])
    def api_narrate(pid):
        """Generate scaffold + narrative for a transcript using Claude Opus 4.7.

        Body: {"path": <source>, "style": "story", "force": false, "language_hint": "en"}
        Returns: {scaffold, narrative, scaffold_path, narrative_path, cached}
        """
        p = registry.get(pid)
        if not p:
            abort(404)
        body = request.get_json(force=True) or {}
        path = (body.get("path") or "").strip()
        style = body.get("style") or "story"
        force = bool(body.get("force", False))
        language_hint = body.get("language_hint") or (
            p.config.language if p.config.language != "auto" else "en"
        )

        if not path:
            return jsonify({"ok": False, "error": "missing_path"}), 400
        tx = Engine.transcript_path_for(Path(path), p.config, "txt")
        if not tx.exists():
            return jsonify({
                "ok": False, "error": "no_transcript",
                "message": "transcript not generated yet — transcribe the file first",
            }), 404

        try:
            result = narrate_mod.narrate(
                tx, style=style, language_hint=language_hint, force=force,
            )
        except narrate_mod.NarrateError as e:
            return jsonify({
                "ok": False, "error": "narrate_failed", "message": str(e),
            }), 500

        return jsonify({"ok": True, **result})

    @app.route("/api/projects/<pid>/prioritize", methods=["POST"])
    def api_prioritize(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        body = request.get_json(force=True)
        state.prioritize(body.get("paths", []))
        worker.wake()
        return jsonify({"ok": True, "queue": state.priority_queue()})

    @app.route("/api/projects/<pid>/retranscribe", methods=["POST"])
    def api_retranscribe(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        body = request.get_json(force=True)
        for path in body.get("paths", []):
            # delete the existing transcript so the engine picks it up again
            tx = Engine.transcript_path_for(Path(path), p.config, "txt")
            srt = Engine.transcript_path_for(Path(path), p.config, "srt")
            try: tx.unlink()
            except FileNotFoundError: pass
            try: srt.unlink()
            except FileNotFoundError: pass
            state.unmark(path)
            state.prioritize([path])
        worker.wake()
        return jsonify({"ok": True})

    @app.route("/api/projects/<pid>/skip", methods=["POST"])
    def api_skip(pid):
        state = registry.state(pid)
        if not state:
            abort(404)
        body = request.get_json(force=True)
        for path in body.get("paths", []):
            state.mark_skipped(path)
        return jsonify({"ok": True})

    # ----- YouTube ingest -----
    # Decision #11: keep these JSON-clean and parameter-stable so the
    # MCP server track can lift them as `transcribe-studio:add_youtube_url`
    # etc. without reshaping the contract.

    _ALLOWED_MODES = ("speech", "music")
    _TERMINAL_URL_STATUSES = ("done", "failed")
    _IN_FLIGHT_URL_STATUSES = ("downloading", "separating", "transcribing")

    @app.route("/api/projects/<pid>/youtube", methods=["GET"])
    def api_youtube_list(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        return jsonify({"urls": state.list_urls()})

    @app.route("/api/projects/<pid>/youtube", methods=["POST"])
    def api_youtube_submit(pid):
        p = registry.get(pid)
        if not p:
            abort(404)
        if not p.youtube_enabled:
            return jsonify({
                "ok": False,
                "error": "youtube_disabled",
                "message": "Enable YouTube ingest in this project's settings first.",
            }), 400
        if not p.folders:
            return jsonify({
                "ok": False,
                "error": "no_folders",
                "message": "Project has no folders configured; YouTube downloads need somewhere to land.",
            }), 400

        body = request.get_json(force=True) or {}
        url = (body.get("url") or "").strip()
        mode = (body.get("mode") or p.youtube_default_mode).strip().lower()

        if not url:
            return jsonify({"ok": False, "error": "missing_url"}), 400
        if not (url.startswith("http://") or url.startswith("https://")):
            return jsonify({
                "ok": False, "error": "invalid_url",
                "message": "URL must start with http:// or https://",
            }), 400
        if mode not in _ALLOWED_MODES:
            return jsonify({
                "ok": False, "error": "invalid_mode",
                "message": f"mode must be one of {_ALLOWED_MODES}",
            }), 400

        state = registry.state(pid)
        row = state.add_url(url, mode)
        worker.wake()
        return jsonify({"ok": True, "url_row": row}), 201

    @app.route("/api/projects/<pid>/youtube/<url_id>", methods=["DELETE"])
    def api_youtube_remove(pid, url_id):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        row = state.get_url(url_id)
        if not row:
            abort(404)
        if row.get("status") in _IN_FLIGHT_URL_STATUSES:
            return jsonify({
                "ok": False,
                "error": "in_flight",
                "message": (
                    f"URL is currently {row['status']}; pause the worker first or wait "
                    f"for the stage to complete before removing."
                ),
            }), 409
        state.remove_url(url_id)
        return jsonify({"ok": True})

    @app.route("/api/projects/<pid>/youtube/<url_id>/prioritize", methods=["POST"])
    def api_youtube_prioritize(pid, url_id):
        """Mark a queued URL as 'up next' — it'll run before other queued rows.

        Body (optional): {"priority": true|false}. Defaults to true.
        Only works on `queued` rows; in-flight rows can't be reordered (the
        worker has already committed to the current one).
        """
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        row = state.get_url(url_id)
        if not row:
            abort(404)
        if row.get("status") != "queued":
            return jsonify({
                "ok": False,
                "error": "not_queued",
                "message": f"can only reorder queued rows; this one is '{row.get('status')}'",
            }), 409
        body = request.get_json(silent=True) or {}
        priority = bool(body.get("priority", True))
        updated = state.update_url(url_id, priority=priority)
        worker.wake()
        return jsonify({"ok": True, "url_row": updated})

    @app.route("/api/projects/<pid>/youtube/<url_id>/retry", methods=["POST"])
    def api_youtube_retry(pid, url_id):
        p = registry.get(pid)
        if not p:
            abort(404)
        state = registry.state(pid)
        row = state.get_url(url_id)
        if not row:
            abort(404)
        if row.get("status") != "failed":
            return jsonify({
                "ok": False,
                "error": "not_failed",
                "message": f"can only retry failed rows; this one is '{row.get('status')}'",
            }), 409
        updated = state.update_url(
            url_id,
            status="queued", stage=None,
            failed_reason=None,
            started_at=None, finished_at=None,
        )
        worker.wake()
        return jsonify({"ok": True, "url_row": updated})

    # ----- audio serving (for Music tab HTML5 players) -----
    @app.route("/api/audio")
    def api_audio():
        """Serve an audio file by absolute path for the in-browser Music tab players.

        Security: only serves files with audio extensions that live under a
        registered project folder. Rejects everything else with 403/404.
        """
        import pathlib as _pathlib
        path = request.args.get("path", "")
        p = _pathlib.Path(path).resolve()
        if not p.exists() or not p.is_file():
            abort(404)
        allowed_suffixes = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
        if p.suffix.lower() not in allowed_suffixes:
            abort(403)
        all_folders = []
        for proj in registry.all():
            all_folders.extend(proj.folders)
        if not any(str(p).startswith(str(_pathlib.Path(f).resolve())) for f in all_folders):
            abort(403)
        mimetype = "audio/mpeg" if p.suffix.lower() == ".mp3" else "audio/wav"
        return send_file(str(p), mimetype=mimetype)

    # ----- engine info (models, etc.) -----
    @app.route("/api/engine")
    def api_engine():
        models_dir = Path.home() / "Documents/cowork-tools/whisper-models"
        installed = []
        if models_dir.exists():
            for f in models_dir.iterdir():
                if f.suffix == ".bin":
                    info = KNOWN_MODELS.get(f.name, {})
                    installed.append({
                        "name": f.name,
                        "size_bytes": f.stat().st_size,
                        **info,
                    })
        return jsonify({
            "models_dir": str(models_dir),
            "installed_models": installed,
            "known_models": KNOWN_MODELS,
        })

    # ----- native macOS folder picker -----
    # Browsers can't open the Finder folder chooser, so we shell out to
    # osascript and return the chosen POSIX path. Cancel → {"cancelled": true}
    # (osascript exits non-zero with "User canceled" on stderr).
    @app.route("/api/dialog/pick-folder", methods=["POST"])
    def api_pick_folder():
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Choose a project folder")'],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            return jsonify({"error": f"osascript failed: {e}"}), 500
        if r.returncode != 0:
            if "User canceled" in (r.stderr or "") or "User cancelled" in (r.stderr or ""):
                return jsonify({"cancelled": True})
            return jsonify({"error": (r.stderr or "osascript returned non-zero").strip()}), 500
        path = (r.stdout or "").strip().rstrip("/")
        if not path:
            return jsonify({"cancelled": True})
        return jsonify({"path": path})

    # ----- reveal file in Finder -----
    # Browsers block window.open("file://...") for security, so the UI's
    # "Show in Finder" button posts here and we shell out to `open -R`.
    # Do NOT stat the path from Python — paths under ~/Documents are TCC-
    # protected and `pathlib.exists()` would trigger the Files-and-Folders
    # permission prompt for Terminal/Python. `open` is a Launch Services
    # shim that reveals via Finder, which has its own TCC scope, so the
    # prompt never appears. Falls back to `open <parent>` if reveal fails
    # (file was deleted but transcript record remains).
    @app.route("/api/reveal", methods=["POST"])
    def api_reveal():
        body = request.get_json(silent=True) or {}
        raw = (body.get("path") or "").strip()
        if not raw:
            return jsonify({"error": "path required"}), 400
        try:
            r = subprocess.run(
                ["open", "-R", raw],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as e:
            return jsonify({"error": f"open failed: {e}"}), 500
        if r.returncode == 0:
            return jsonify({"ok": True, "revealed": raw})
        parent = str(Path(raw).parent)
        try:
            r2 = subprocess.run(
                ["open", parent],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as e:
            return jsonify({"error": f"open failed: {e}"}), 500
        if r2.returncode == 0:
            return jsonify({"ok": True, "opened_parent": parent})
        return jsonify({
            "error": (r.stderr or r2.stderr or "open returned non-zero").strip(),
            "path": raw,
        }), 404

    # ----- log tail -----
    @app.route("/api/log")
    def api_log():
        n = int(request.args.get("n", 100))
        if not LOG_PATH.exists():
            return jsonify({"lines": []})
        with open(LOG_PATH) as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in lines[-n:]]})

    return app, registry, worker


def _summary(p: Project, registry: Registry) -> dict:
    state = registry.state(p.id)
    rows = annotate_with_state(scan_project(p, state), state)
    counts = {"completed": 0, "failed": 0, "pending": 0, "skipped": 0, "in_progress": 0, "queued": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total = len(rows)

    # YouTube inbox summary — count by status so the UI can render a
    # badge without fetching the full list. Order chosen to match the
    # row lifecycle for easy "where in pipeline" debugging.
    yt_counts = {"queued": 0, "downloading": 0, "separating": 0,
                 "transcribing": 0, "done": 0, "failed": 0}
    for row in state.list_urls():
        s = row.get("status")
        if s in yt_counts:
            yt_counts[s] += 1

    return {
        "id": p.id,
        "name": p.name,
        "auto_run": p.auto_run,
        "ordering": p.ordering,
        "model": p.config.model,
        "language": p.config.language,
        "translate": p.config.translate_to_english,
        "vad": p.config.vad,
        "no_context": p.config.no_context,
        "total": total,
        "counts": counts,
        "progress": (counts["completed"] / total) if total else 0,
        "folders": p.folders,
        "required_volumes": p.required_volumes,
        # YouTube ingest
        "youtube_enabled": p.youtube_enabled,
        "youtube_default_mode": p.youtube_default_mode,
        "youtube_counts": yt_counts,
        "youtube_total": sum(yt_counts.values()),
    }


def _full(p: Project, registry: Registry) -> dict:
    return {**p.to_dict(), "summary": _summary(p, registry)}


def main():
    app, registry, worker = create_app()
    worker.start()
    # Auto-open browser if no other instance is running this port
    if "--no-browser" not in sys.argv:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                already_running = (s.connect_ex((HOST, PORT)) == 0)
        except Exception:
            already_running = False
        if not already_running:
            threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
