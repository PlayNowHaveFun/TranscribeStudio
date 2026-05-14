"""
Gate 2 smoke test for app/yt_ingest.

Two parts:
  1. Pure-function tests (sanitize, target_folder) — no network, no
     subprocess. Run anytime: `python tests/smoke_yt_ingest.py --unit`.
  2. End-to-end ingest against a real YouTube URL — hits the network,
     spawns yt-dlp / demucs, writes files to disk. Run with:
       python tests/smoke_yt_ingest.py speech <url> [out_dir]
       python tests/smoke_yt_ingest.py music  <url> [out_dir]

Default out_dir is ./smoke_out under the repo root. Wipe it between
runs if you want a clean slate.

Per GATE_CHECKIN.md decision #9 we never built a CLI for production
use — this script is solely for Gate 2 validation. It's a test
harness, not a user surface.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this file directly (python tests/smoke_yt_ingest.py ...)
# from the repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.yt_ingest import (
    sanitize_title, target_folder, YtIngest, YtIngestConfig,
)


# --------------------------------------------------------------------------
# 1. Pure-function tests — fast, no side effects
# --------------------------------------------------------------------------

def test_sanitize_title():
    cases = [
        ("Plain English Title",        "Plain English Title"),
        ("Title: with/colons",         "Title- with-colons"),
        ("..leading dots",             "leading dots"),
        ("trailing dots..",            "trailing dots"),
        ("   spaced   out   ",         "spaced out"),
        ("",                           "untitled"),
        ("///",                        "untitled"),
        ("name\nwith\tcontrol chars",  "name-with-control chars"),
    ]
    failures = []
    for inp, want in cases:
        got = sanitize_title(inp)
        if got != want:
            failures.append((inp, want, got))
    if failures:
        print("FAIL  sanitize_title:")
        for inp, want, got in failures:
            print(f"      input:  {inp!r}")
            print(f"      want:   {want!r}")
            print(f"      got:    {got!r}")
        return False
    # length cap
    long_title = "x" * 500
    capped = sanitize_title(long_title)
    if len(capped) > 180:
        print(f"FAIL  sanitize_title length cap: got {len(capped)} chars")
        return False
    print("PASS  sanitize_title")
    return True


def test_target_folder(tmp_root: Path):
    # fresh title -> bare folder
    f1 = target_folder(tmp_root, "First Song", "abc12345678")
    if f1.name != "First Song":
        print(f"FAIL  target_folder: expected 'First Song', got {f1.name!r}")
        return False
    # create it on disk to force a collision
    f1.mkdir(parents=True, exist_ok=True)
    # same title + same video_id -> should still collide and get a hash
    f2 = target_folder(tmp_root, "First Song", "abc12345678")
    if "_" not in f2.name or f2.name == "First Song":
        print(f"FAIL  target_folder collision: expected hash suffix, got {f2.name!r}")
        return False
    # different video_id -> different hash
    f3 = target_folder(tmp_root, "First Song", "xyz98765432")
    if f3 == f2:
        print(f"FAIL  target_folder: different video_id should yield different folder")
        return False
    # cleanup
    f1.rmdir()
    print("PASS  target_folder")
    return True


def run_unit_tests() -> int:
    import tempfile
    ok = True
    ok &= test_sanitize_title()
    with tempfile.TemporaryDirectory() as tmp:
        ok &= test_target_folder(Path(tmp))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 2. End-to-end ingest — touches yt-dlp / demucs / network / disk
# --------------------------------------------------------------------------

def run_e2e(mode: str, url: str, out_dir: Path) -> int:
    if mode not in ("speech", "music"):
        print(f"unknown mode: {mode}")
        return 2

    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"--- ingest mode={mode} url={url}")
    print(f"--- out_dir={out_dir}")

    ingester = YtIngest()
    config = YtIngestConfig(mode=mode)

    final_event = None
    last_pct = {"download": -1, "separate": -1}
    # Rolling tail so we can dump it when a `fail` event arrives — the
    # actual error message from yt-dlp / demucs lives in stderr which
    # we route through these log events.
    from collections import deque
    log_tail = deque(maxlen=40)

    for ev in ingester.ingest(url, out_dir, config):
        if ev.phase == "log":
            line = ev.payload.get("line", "")
            stage = ev.payload.get("stage", "?")
            if line:
                log_tail.append(f"  [{stage}] {line}")
            continue
        if ev.phase == "download_progress":
            pct = int(ev.payload.get("percent", 0))
            if pct - last_pct["download"] >= 10:
                print(f"  download  {pct:>3}%")
                last_pct["download"] = pct
            continue
        if ev.phase == "separate_progress":
            pct = int(ev.payload.get("percent", 0))
            if pct - last_pct["separate"] >= 10:
                print(f"  separate  {pct:>3}%")
                last_pct["separate"] = pct
            continue
        # print everything else
        payload_brief = {
            k: v for k, v in ev.payload.items() if k != "line"
        }
        print(f"[{ev.phase:<16}] {payload_brief}")
        final_event = ev

    if final_event is None:
        print("FAIL  ingest yielded no events")
        return 1
    if final_event.phase == "fail":
        print(f"\nFAIL  ingest ended in failure")
        print(f"  stage:  {final_event.payload.get('stage')}")
        print(f"  reason: {final_event.payload.get('reason')}")
        if log_tail:
            print(f"\n--- last {len(log_tail)} log lines from the failing stage ---")
            for line in log_tail:
                print(line)
        return 1
    if final_event.phase != "done":
        print(f"FAIL  ingest ended on unexpected phase {final_event.phase!r}")
        return 1

    folder = Path(final_event.payload["folder"])
    print(f"\n--- verifying outputs in {folder}")
    expected = ["source.mp3", "metadata.json"]
    if mode == "music":
        expected += ["vocals.mp3", "instrumental.mp3"]
        if (folder / "_demucs").exists():
            print(f"FAIL  _demucs/ should have been cleaned up")
            return 1
    missing = [name for name in expected if not (folder / name).exists()]
    if missing:
        print(f"FAIL  missing files: {missing}")
        return 1

    for name in expected:
        size = (folder / name).stat().st_size
        print(f"  {name:<20} {size:>12,} bytes")
    print("\nPASS  end-to-end")
    return 0


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--unit", action="store_true",
                   help="run pure-function tests only (no network)")
    p.add_argument("mode", nargs="?", choices=("speech", "music"),
                   help="ingest mode for e2e run")
    p.add_argument("url",  nargs="?", help="YouTube URL")
    p.add_argument("out_dir", nargs="?", default=str(REPO_ROOT / "smoke_out"),
                   help="output root (default: ./smoke_out)")
    args = p.parse_args()

    if args.unit:
        sys.exit(run_unit_tests())
    if not args.mode or not args.url:
        p.print_help()
        sys.exit(2)
    sys.exit(run_e2e(args.mode, args.url, Path(args.out_dir)))


if __name__ == "__main__":
    main()
