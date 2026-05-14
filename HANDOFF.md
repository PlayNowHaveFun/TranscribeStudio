# Transcribe Studio — Handoff for a fresh Cowork task

This is the orientation doc for any new Claude session that starts work on
this folder. Read this first. It tells you what's here, what's been decided,
what's working, and where to plug in for new features.

---

## Where everything lives

**Filesystem location:** `/Users/ruchir/Documents/cowork-tools/transcribe-studio/`

**Studio URL when running:** `http://127.0.0.1:5180`

**Companion folder (read-only, contains the user's first project's data and
the related strategy docs):** `/Users/ruchir/Documents/Art/Samvaad/`

**Models folder (whisper.cpp models live here, shared with the engine):**
`/Users/ruchir/Documents/cowork-tools/whisper-models/`
- `ggml-large-v3.bin` (~3 GB — multilingual, default)
- `ggml-silero-v6.2.0.bin` (~670 MB — VAD)

**LaunchAgent plist (when installed):** `~/Library/LaunchAgents/com.transcribestudio.plist`

---

## Folder layout

```
transcribe-studio/
├── HANDOFF.md                           ← THIS FILE
├── README.md                            ← user-facing overview, install steps
├── AGENT_APPROACH_BRAINSTORM.md         ← thinking doc on the future agent layer
├── requirements.txt                     ← Flask>=3.0
│
├── app/                                 ← Python source
│   ├── __init__.py                      (empty package marker)
│   ├── main.py                          ← Flask app + all HTTP routes (17 routes)
│   ├── transcriber.py                   ← Worker thread, queue, status tracking
│   ├── engine.py                        ← Whisper.cpp orchestrator (Python port of original transcribe.sh)
│   ├── projects.py                      ← Project + ProjectState + Registry (JSON-backed)
│   ├── scanner.py                       ← File discovery + ordering + status annotation
│   ├── conditions.py                    ← AC power, battery %, volume mounted checks
│   ├── templates/
│   │   └── index.html                   ← single-page UI shell
│   └── static/
│       ├── style.css                    ← clean modern aesthetic
│       └── app.js                       ← polling-based UI controller (no framework)
│
├── bin/
│   └── transcribe-engine.sh             ← original bash engine, kept as REFERENCE only
│                                          (the live engine is app/engine.py — bash version not invoked)
│
├── data/                                ← runtime state
│   ├── projects.json                    ← registry of all projects
│   └── projects/<id>/state.json         ← per-project queue/completed/failed/priority
│
├── logs/                                ← all logs go here
│   ├── studio.log                       ← main app log
│   ├── launchd.out.log
│   ├── launchd.err.log
│   └── app-launch.log
│
├── venv/                                ← Python virtualenv (created by setup.sh)
│
├── Transcribe Studio.app/               ← Mac app bundle
│   └── Contents/
│       ├── Info.plist                   (CFBundleIdentifier: com.ruchir.transcribestudio)
│       └── MacOS/
│           └── TranscribeStudio         ← shell-script entry point
│
├── com.transcribestudio.plist           ← launchd agent template (REPLACE_WITH_HOME placeholder)
├── run.sh                               ← entry point: cd /tmp; activate venv; python -m app.main
├── setup.sh                             ← one-time: create venv, install Flask, validate whisper-cli + ffmpeg
│
└── *.command                            ← double-click installers/controls
    ├── Install.command                  (full first-time install)
    ├── Repair.command                   (re-bootstrap launchd agent if Install had issues)
    ├── Open Studio.command              (open browser to the running URL, start backend if needed)
    └── Uninstall.command                (stop and remove agent, leaves files alone)
```

---

## Design philosophy (decided, do not relitigate)

Three decisions are now load-bearing. Don't reverse them without explicit user
sign-off.

**1. The Studio does ONE thing: transcription.** It scans folders, runs
whisper.cpp, writes `.txt` and `.srt` to `<source>/transcriptions/`. It does
**not** do tagging, summarization, theme detection, or AI analysis. Those
belong to the *Mental Engine* — a separate app, not yet built. See the
brainstorm docs in `~/Documents/Art/Samvaad/` for the full architecture.

**2. Multi-project from the start.** Samvaad (therapy) is project #1, but
the Studio is generic — any folder of audio/video can be a project. Each
project has its own whisper config (model, language, translate, VAD,
no-context, chunk thresholds), its own queue, its own state, its own
auto-run flag. New project types coming: Music, Acting practice, YouTube,
Brainstorms, Blog, Pitch projects.

**3. Local-first, privacy-first.** The Studio doesn't send anything to any
cloud. Whisper.cpp runs on the user's Mac. Models are local. No telemetry.
A separate Mental Engine *might* opt-in to occasional cloud Claude calls,
but the Studio itself stays local-only.

---

## How it actually runs

### Process model

A single Python process started by `run.sh`:
1. `cd /tmp` (TCC-safe working directory — protects whisper-cli + ffmpeg from
   getcwd failures when CWD is in TCC-protected `~/Documents`)
2. `export PYTHONPATH=$STUDIO_ROOT` (so `python -m app.main` finds the package)
3. `exec venv/bin/python -m app.main`

The Python process:
- Starts Flask on `127.0.0.1:5180` (the only port the Studio uses)
- Spawns a single Worker thread (`app/transcriber.py`) that loops:
  pick next pending file → run engine → log result → repeat
- Worker respects per-project conditions (AC power, required volumes, paused flag)
- Engine spawns subprocess for `whisper-cli` and `ffmpeg/ffprobe` (CWD inherited from /tmp)

### LaunchAgent

`com.transcribestudio.plist` runs `run.sh` at login with `KeepAlive=true`.
Working directory `/tmp`, PATH includes `/opt/homebrew/bin` and
`/usr/local/bin` so brew-installed binaries are findable.

**Install/uninstall use `launchctl bootstrap`/`bootout`** (modern macOS),
NOT the deprecated `launchctl load`/`unload`. Earlier versions used `load`
and that broke on Sequoia. If you find load/unload anywhere outside
`Repair.command`'s legacy fallback, replace with bootstrap/bootout.

### Required macOS permissions

Both of these need Full Disk Access (System Settings → Privacy & Security):
1. `/bin/bash` — for the launchd-spawned shell
2. The user's Python binary (e.g. `/usr/local/bin/python3` or `/opt/homebrew/bin/python3.12`)

OR add `Transcribe Studio.app` to FDA, which covers the .app launch path
but not the launchd path. For belt-and-suspenders, add all three.

Without FDA, you'll see `Operation not permitted` on `pyvenv.cfg` reads
or `getcwd` calls. Both come from TCC.

---

## Engine architecture (`app/engine.py`)

The engine is a Python port of the original `bin/transcribe-engine.sh`,
preserving all the audio-transcribe-skill learnings:

- **Default model**: `ggml-large-v3.bin` (multilingual, hallucination-resistant)
- **VAD on by default**: Silero v6, kills silence-driven hallucination loops
- **`max_len=80`**: limits segment length so any surviving loop stays shallow
- **Auto-chunking** for files > `chunk_threshold_min` (default 10 min) into
  `chunk_min`-minute pieces (default 5). Per-chunk whisper runs that get
  merged with timestamp offsets.
- **Knobs that map directly to whisper-cli flags**: model, language, translate
  (`-tr`), VAD (`--vad -vm`), no-context (`--no-context`)
- **Idempotent**: skip if `<output_dir>/<basename>.txt` already exists
- **Fail-soft on chunked path**: a single bad chunk doesn't kill the whole
  transcription; merge what's there

**Yields events** as it runs: `Event(phase, file, payload, timestamp)`.
Phases: `preflight`, `extract`, `transcribe`, `split`, `split_done`, `chunk`,
`chunk_skip`, `chunk_fail`, `merge`, `done`, `fail`, `skip`, `log`.
The Worker consumes these and the UI surfaces them as the live status.

**Hallucination detection** is built into the engine output (`detect_hallucinations`)
— consecutive line repetition + low diversity ratio. UI surfaces a warning
banner on transcripts that fail the check.

### Engine subprocess CWD

The engine's `_run` always sets `cwd="/tmp"` for subprocesses. This is the
single most important detail in the engine. Don't change it without
understanding the TCC issue (see logs of past failures in `logs/launchd.err.log`
if you need a refresher).

---

## Data model

### Project (in `app/projects.py`, persisted to `data/projects.json`)

```python
@dataclass
class Project:
    id: str                    # slug, e.g. "samvaad"
    name: str                  # display name
    folders: list[str]         # absolute paths
    config: WhisperConfig      # all whisper knobs
    auto_run: bool = True
    require_ac_power: bool = True
    required_volumes: list[str] = []   # e.g. ["/Volumes/Offline_Database"]
    exclude_patterns: list[str] = []   # globs: ["test_clip_*", ".*"]
    ordering: str = "newest_first"     # newest_first | oldest_first | alpha
    created_at: str
    notes: str
```

### ProjectState (per project, in `data/projects/<id>/state.json`)

```python
{
    "completed": {path: {completed_at, duration_sec}},
    "failed":    {path: {failed_at, reason, attempts}},
    "skipped":   {path: {skipped_at}},
    "priority_queue": [paths],   # user-picked, processed first
    "current": path | null,      # in-progress
}
```

### WhisperConfig

All the whisper-cli knobs as a dataclass — see `app/engine.py:WhisperConfig`.
The `KNOWN_MODELS` dict has 10 entries with size/lang/quality/speed metadata
the UI uses for the model picker.

---

## HTTP API (17 routes, all in `app/main.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI (single page) |
| GET | `/api/status` | Worker + projects + system snapshot (UI polls every 2s) |
| POST | `/api/pause` / `/api/resume` / `/api/wake` | Worker controls |
| GET | `/api/projects` | List all projects |
| POST | `/api/projects` | Create a project |
| GET / PATCH / DELETE | `/api/projects/<pid>` | Per-project CRUD |
| GET | `/api/projects/<pid>/files` | All files in project's folders, with status |
| GET | `/api/projects/<pid>/transcript?path=...` | Read a transcript + quality assessment |
| POST | `/api/projects/<pid>/prioritize` | Move paths to front of queue |
| POST | `/api/projects/<pid>/retranscribe` | Delete .txt and re-queue |
| POST | `/api/projects/<pid>/skip` | Mark paths as skipped |
| GET | `/api/engine` | List installed + known models |
| GET | `/api/log?n=100` | Tail of `logs/studio.log` |

Adding a new feature usually means: add a route, add a method to
`Worker` or `Engine` if it's behavioral, add a section to `app/static/app.js`
+ `index.html` for the UI.

---

## UI architecture (`app/static/app.js`)

Single-page app, vanilla JS, no framework. Key design points:

- **Two render paths**: `renderContentStructure()` builds the DOM once when
  the view changes; `updateLiveData()` runs every poll and just patches the
  dynamic bits (metric numbers, log tail, current-file phase). This is what
  killed the flicker that earlier versions had.
- **`setText(id, val)` helper** is a no-op when value hasn't changed —
  prevents needless DOM mutations.
- **File list cached** in JS, refreshed only every 15s or when worker
  status transitions. Filters and search apply to the cache (no extra
  HTTP).
- **Modals are mutually exclusive** — `closeAllModals()` runs before
  any `openModal(id)`. ESC closes. Backdrop click closes.
- **Log tail preserves user scroll position** — only auto-scrolls if user
  was already at bottom.

Don't add a framework to the frontend. The current vanilla approach is
~700 lines and handles everything.

---

## Current status (as of 2026-05-02)

- ✓ Studio is built end-to-end and verified
- ✓ Samvaad pre-registered as project #1 on first run (auto-bootstrap)
- ✓ launchd agent installs cleanly via `Install.command` or `Repair.command`
- ✓ FDA permission requirements documented in `README.md`
- ✓ UI flicker fixed (live-update path separated from structure render)
- ✓ Modal stacking fixed (mutual exclusion + ESC + backdrop click)
- ✓ TCC issues solved by `cwd=/tmp` everywhere

**Pending features (good first tasks):**
1. **Settings UI** — currently only basics editable inline; no real
   project-settings modal. The `openSettingsModal` placeholder uses a
   `confirm()` dialog. Build a proper modal that exposes all WhisperConfig
   fields.
2. **Folder picker** — currently you paste folder paths in the New Project
   modal. A native folder picker (via small backend helper running `osascript`
   "choose folder") would be friendlier.
3. **Bulk file actions** — selecting multiple rows in the file list to
   prioritize/retranscribe/skip.
4. **Concurrency knob** — `MAX_CONCURRENT_FILES` setting in the UI.
   Currently hardcoded to 1 (one whisper-cli at a time). Wiring up
   2-N parallel transcriptions is a worker-level change.
5. **Recent activity feed** — timeline view of completed/failed in the
   last 24h. Data is already in `state.json`.
6. **Manifest export** — generate `<project>/manifest.json` listing all
   transcripts with metadata. Becomes the input to the future Mental
   Engine. Important — see "Why" below.
7. **`.app` icon** — currently no `Resources/icon.icns`. A simple icon
   would make Finder less generic-looking.

**Known issues:**
- The 6 newest Samvaad transcripts initially landed but state.json didn't
  show 4 as "completed" because the Studio first-run boot picks up
  existing transcripts via the scanner's `has_transcript` check, but the
  state file doesn't get backfilled. Worth fixing — see scanner's
  `annotate_with_state` function for the patch point.

---

## How to start a feature

1. Assume the Studio is running (background launchd agent). Don't restart
   it unless you change Python imports or the engine's subprocess
   handling.
2. For UI-only changes: edit `app/static/app.js` or
   `app/templates/index.html`, then **Cmd+R in the browser** to reload —
   no agent restart needed.
3. For backend changes: edit Python files, then either restart the agent
   (`launchctl bootout gui/$(id -u)/com.transcribestudio && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.transcribestudio.plist`)
   or just kill the python process and let `KeepAlive=true` restart it
   (`pkill -f 'app.main'`).
4. Test in the browser. The Worker thread keeps running in the background
   even while you're hitting routes — the UI shows the current
   transcribing file in the "Now transcribing" card.
5. Logs to watch:
   - `logs/studio.log` — main app + worker events
   - `logs/launchd.err.log` — anything that crashed before main.py logging took over

---

## What NOT to do

- **Don't add tagging, summarization, theme detection, AI analysis.** Those
  belong to the future Mental Engine — a separate app. Keeping the Studio
  scope tight is what makes it reliable.
- **Don't change the engine's subprocess CWD** away from `/tmp` without
  understanding the TCC issue.
- **Don't add a frontend framework.** Vanilla works.
- **Don't make whisper-cli decisions for the user**. Surface model knobs in
  the UI; let them choose.
- **Don't add cloud calls.** The Studio is local-only by design. If a feature
  requires cloud, it doesn't belong in the Studio.

---

## Related design docs (in `~/Documents/Art/Samvaad/`)

These are about the *consumer* of the Studio (the future Mental Engine).
They don't constrain Studio work, but reading them tells you what the
Studio's outputs are eventually feeding:

- `STRATEGY.md` — three-layer architecture (Studio → Mental Engine → Reflection)
- `MENTAL_ENGINE_BRAINSTORM.md` — what the consumer layer does
- `LOCAL_FIRST_ENGINE.md` — running the consumer locally for privacy
- `DASHBOARD_BRAINSTORM.md` — one of the Mental Engine's outputs

The Studio's job is to produce clean transcripts. Everything else is
downstream.

---

## How to brief a fresh Cowork session

If you want to start a new task on the Studio, paste this minimal
prompt to the new session:

> I'm working on the Transcribe Studio — a local Mac app that
> transcribes audio/video across multiple projects using whisper.cpp.
> It lives at `~/Documents/cowork-tools/transcribe-studio/`. Read
> `HANDOFF.md` first; that has the architecture, design constraints,
> what's working, and what's pending. Then [describe the specific
> feature you want to add].

That's enough for the new Claude to come up to speed in a few seconds
and start work without re-deriving the existing architecture.
