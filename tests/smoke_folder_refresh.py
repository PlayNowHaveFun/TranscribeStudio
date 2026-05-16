"""Smoke test for folder management: startup scan + watcher + refresh
helpers correctly classify newly-dropped files as 'pending'.

Run from the repo root:
    python tests/smoke_folder_refresh.py

Exits 0 on success, 1 on any assertion failure. Patches DATA_DIR to a
tempdir so it never touches the user's real data/ directory.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_data = tmp_path / "data"
        fake_data.mkdir()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        fake_log = tmp_path / "studio.log"

        # Patch DATA_DIR / PROJECTS_FILE before instantiating Registry. The
        # Project.state_dir property and Registry._save read DATA_DIR /
        # PROJECTS_FILE lazily, so this redirects everything to tmp.
        from app import projects as projects_mod
        projects_mod.DATA_DIR = fake_data
        projects_mod.PROJECTS_FILE = fake_data / "projects.json"

        from app.projects import Project, Registry
        from app.engine import WhisperConfig
        from app.main import _scan_for_new_files, _startup_scan
        from app.watcher import Watcher

        registry = Registry()
        proj = Project(
            id="smoke",
            name="Smoke",
            folders=[str(media_dir)],
            config=WhisperConfig(),
            auto_run=True,
            require_ac_power=False,
        )
        registry.add(proj)
        state = registry.state(proj.id)

        # 1. Empty folder → no new files
        assert _scan_for_new_files(proj, state) == [], "empty folder must yield no new files"
        print("[ok] empty folder: 0 new files")

        # 2. Drop a fake .mp3 (zero-byte placeholder; we never invoke whisper)
        new_file = media_dir / "dropped_while_quit.mp3"
        new_file.write_bytes(b"")
        found = _scan_for_new_files(proj, state)
        assert str(new_file) in found, f"new file not detected: found={found}"
        print(f"[ok] new file detected: {new_file.name}")

        # 3. Startup scan should run cleanly and write to the log.
        _startup_scan(registry, fake_log)
        assert fake_log.exists(), "startup scan must write the log file"
        log_text = fake_log.read_text()
        assert "STARTUP scan" in log_text, f"summary line missing: {log_text!r}"
        print("[ok] _startup_scan wrote summary line")

        # 4. Watcher.tick() — stub worker confirms wake() fires for new files.
        class StubWorker:
            woken = 0
            def wake(self):
                self.woken += 1

        stub = StubWorker()
        w = Watcher(registry, stub, fake_log)
        w.tick()
        assert stub.woken == 1, f"watcher should wake once when new files exist, got {stub.woken}"
        print("[ok] Watcher.tick() found new files and called worker.wake()")

        # 5. After we mark the file completed, a second tick should NOT wake.
        state.mark_completed(str(new_file))
        stub.woken = 0
        w.tick()
        assert stub.woken == 0, "watcher should not wake when nothing is new"
        assert _scan_for_new_files(proj, state) == [], "completed file should not appear as new"
        print("[ok] completed file no longer counted as new")

        # 6. Non-audio files are ignored.
        (media_dir / "notes.txt").write_text("not media")
        assert _scan_for_new_files(proj, state) == [], "non-media file must not be picked up"
        print("[ok] non-media files ignored")

    print("\nAll folder-refresh smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
