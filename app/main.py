"""
Transcribe Studio — Flask backend.

Run with:
    python -m app.main          (from STUDIO_ROOT)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, render_template, send_file, abort

from . import conditions
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
        rows = annotate_with_state(scan_project(p), state)
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
        else:
            out["txt"] = None
        if srt.exists():
            out["srt_path"] = str(srt)
        return jsonify(out)

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
    rows = annotate_with_state(scan_project(p), state)
    counts = {"completed": 0, "failed": 0, "pending": 0, "skipped": 0, "in_progress": 0, "queued": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total = len(rows)
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
