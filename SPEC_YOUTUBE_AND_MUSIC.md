# Spec — YouTube ingest & music mode (lyric/karaoke split)

Status: **draft, pre-gate-checkin** (see `GATE_CHECKIN.md`)
Owner: Ruchir
Last updated: 2026-05-13

Adapts the standalone `yt-tool` CLI spec into a first-class Transcribe Studio
feature. Same two modes — **speech** (URL → audio → transcript) and **music**
(URL → audio → vocal/instrumental split → optionally transcribe vocals as
lyrics) — but plugged into the studio's existing project model, queue,
worker, and UI instead of living as a side-channel bash script.

## Goal & scope

Two operator-visible capabilities:

1. **Speech mode (default).** Paste a YouTube URL into a project → studio
   downloads audio → engine transcribes it → transcript shows up in the
   project just like any other file. Most use case: podcast episodes,
   interviews, lectures.
2. **Music mode (`mode=music`).** Paste a YouTube URL → studio downloads
   audio → Demucs separates `vocals` + `instrumental` → studio optionally
   transcribes `vocals.mp3` as lyrics. Use case: building a personal
   karaoke library (instrumental track + a synced `.srt` lyric file).

**Out of scope for v1** (called out so they don't bleed into the build):

- Karaoke video rendering (instrumental + burned-in subtitles → `.mp4`)
- ~~Playlist support~~ — **shipped** (May 2026, public/unlisted only via
  yt-dlp `--flat-playlist`; private playlists requiring OAuth are still v2).
  A pasted playlist URL becomes a whole new project with each video queued.
  See `INTEGRATION.md` §7.3 and `bin/ts playlist <url>`.
- Lyric forced-alignment polish (`whisperX` etc.)
- Per-file auto-detect of speech vs music
- Non-YouTube URLs (yt-dlp supports plenty of sites; we don't promise them)

## Why this lives in Transcribe Studio (not a side-channel CLI)

The original spec made the right call for a one-off tool: a bash script
that calls `transcribe.sh`. Now that the studio exists, the calculus
flips:

- The studio already has the queue, the conditions engine (AC power,
  required volumes), the live status UI, the hallucination detector,
  per-project whisper config, and idempotent re-runs. A YouTube CLI
  would re-implement all of those badly.
- Transcripts the studio produces are visible alongside everything else
  in one place — same project view, same `redo`/`re-transcribe` button,
  same quality assessment banner.
- Conditions matter for music mode too: Demucs is heavy enough that you
  want it on AC power.
- One-click ops (paste URL, hit Add) is materially better than a
  terminal invocation for the music/karaoke use case, where the user is
  usually not at a shell.

No standalone CLI **for the ingest pipeline itself**. The ingest logic is a Python module (`app/yt_ingest.py`,
sibling to `engine.py` / `scanner.py`) imported by the worker. Decision
locked: the studio is the only entry point — shell-script invocation
isn't a v1 use case, and skipping it keeps the module free of argparse
clutter, import-side-effect hygiene, and Flask-vs-CLI dependency split
concerns.

(A `bin/ts` CLI does exist as of May 2026, but it's a **client** of the
HTTP API — it doesn't re-implement ingest, it just shells `POST /api/projects/<pid>/youtube`
and the playlist endpoints. The studio must be running. See `INTEGRATION.md` §7.)

## How it grafts onto the existing model

The user-facing decision: **per-project URL inbox**. Every project gains
a list of pending YouTube URLs in addition to its scanned folders.
Downloaded audio lands inside one of the project's folders (specifically
in a project-owned `youtube/` subdir), so the existing scanner picks it
up with zero changes to scanning logic.

```
                                  ┌── (existing) folder scanner ──┐
  Project ──┬── folders[]   ──────┘                                ├──► WhisperConfig ──► Engine
            └── youtube_urls[] ──► download → (demucs) → file ─────┘
                                      └─ new stages in the same Worker
```

Concretely, on submission of a URL:

1. URL is appended to the project's `youtube_urls` list in `state.json`.
2. The worker, on its next tick, sees a pending URL and runs the
   download stage (`yt-dlp`), then optionally the separation stage
   (`demucs --two-stems=vocals`), depositing files in the project's
   `youtube/` folder.
3. Once the audio file exists on disk, the existing scanner picks it
   up as a normal media file and the existing engine transcribes it.
4. State for the URL transitions: `queued → downloading → separating
   → transcribing → done` (or `failed` with reason at any stage).

This means: one worker, sequential, same pause/resume/condition logic.
The whole flow inherits the studio's existing reliability properties.

## New data model

Single additive change to `Project` and `ProjectState`. No breaking
changes to the on-disk schema; existing `projects.json` keeps working.

### Project additions (in `app/projects.py`)

```python
@dataclass
class Project:
    # ...existing fields...
    youtube_enabled: bool = False             # gates the URL inbox UI
    youtube_default_mode: str = "speech"      # "speech" | "music"
    youtube_subdir: str = "youtube"           # relative to the *first* folder in folders[]
    music_keep_vocals: bool = True            # delete vocals.mp3 after transcribe if False
    music_force_no_context: bool = True       # safer default for lyrics (whisper loops)
    music_demucs_segment: int = 0             # 0 = no segmenting; raise to 7 if OOM
```

Rationale per field:

- `youtube_enabled` is a feature gate at the project level — you opt
  each project in. Avoids cluttering the UI of projects that will never
  see a URL.
- `youtube_default_mode` lets a "karaoke" project default to music
  while a "podcasts" project defaults to speech. Per-URL submission can
  still override it.
- `youtube_subdir` defaults to `youtube` inside `folders[0]`. If a
  project has multiple folders (e.g. internal + external drive), v1
  always writes to the first one. Multi-folder routing is v2.
- `music_force_no_context: true` is opinionated. Whisper.cpp's
  hallucination loops are the #1 mode of failure on isolated vocal
  stems (long musical interludes look like silence/noise). The studio
  already exposes `no_context` as a knob; we just force it for music.
  Knob is on the project, can be turned off.

### ProjectState additions (in `app/projects.py`)

```python
# state.json gains:
{
  "youtube_urls": [
    {
      "id": "uuid",
      "url": "https://youtube.com/watch?v=...",
      "submitted_at": "2026-05-13T10:00:00",
      "mode": "speech",                       # or "music"
      "title": null,                          # filled after metadata fetch
      "video_id": null,
      "status": "queued",                     # see lifecycle below
      "stage": null,                          # downloading|separating|transcribing
      "target_file": null,                    # absolute path once known
      "metadata_path": null,                  # ~/.../youtube/<title>/metadata.json
      "failed_reason": null,
      "started_at": null,
      "finished_at": null
    }
  ]
}
```

Status lifecycle:

```
queued → downloading → (music only) separating → transcribing → done
                                                  └──► failed (any stage)
```

The transcription step is *not* re-implemented — once the audio file
lands on disk, the existing `Engine.transcribe()` takes over and the
existing state-tracking handles `completed`/`failed`. The URL row's
`status` is set to `done` when the engine emits `done` for the target
file.

### New events from the engine

`engine.py`'s `Event` already carries a `phase` field. We add new
phase values for the ingest stages so the live status UI can render
them without parsing log lines:

- `download_start`, `download_progress`, `download_done`
- `separate_start`, `separate_progress`, `separate_done`

(Existing phases — `preflight`, `extract`, `transcribe`, `chunk`,
`merge`, `done`, `fail`, `skip`, `log` — stay as-is. The UI's live
panel already handles unknown phases gracefully.)

## Pipeline stages — same worker, new stages

The serial worker becomes responsible for three kinds of jobs:

| Job kind          | Picked when                                          | Runs                                       |
|-------------------|------------------------------------------------------|--------------------------------------------|
| URL download      | a `youtube_urls` row is `queued`                     | yt-dlp → writes audio + metadata to disk   |
| URL separation    | a row is `downloading`-done and `mode == "music"`    | demucs → writes `vocals.mp3`, `instrumental.mp3` |
| File transcribe   | scanner finds an untranscribed file (existing path)  | unchanged                                  |

Crucially, **`scanner.next_pending` is the only thing that ever picks
"what's next."** We extend it: it now considers both pending URLs
(returning a URL job) and pending files (returning a file job), with
URLs ordered by submission time, intermixed with the project's existing
file ordering. Default rule: URL jobs run before file jobs for the
same project, because you're usually in the middle of something when
you paste a URL and want it to start now. This is configurable per
project (`prioritize_url_jobs: bool`, default `true`).

Trade-off: a long Demucs run (10+ minutes on a long song) blocks the
transcription queue. This was the deliberate concurrency choice (see
gate doc); the assumption is that for typical use — pasting one URL at
a time, not draining a 200-song playlist — the block is a non-issue.
If it becomes one, the v2 option is to split the worker into an "ingest
worker" + "transcribe worker" with separate `caffeinate` budgets.

## Folder layout (per project)

For a project rooted at `~/Documents/Art/KaraokeLibrary/`:

```
~/Documents/Art/KaraokeLibrary/
└── youtube/                              # the project's youtube_subdir
    └── <sanitized-video-title>/
        ├── metadata.json                 # yt-dlp --print-json output, trimmed
        ├── source.mp3                    # full audio, 320 kbps
        ├── instrumental.mp3              # music mode only
        ├── vocals.mp3                    # music mode only (deletable per setting)
        └── transcriptions/               # produced by the existing engine
            ├── source.txt                # speech mode
            ├── source.srt
            ├── vocals.txt                # music mode (transcript of vocals.mp3)
            └── vocals.srt                # music mode (synced lyric file)
```

Key design choices:

- The folder for a given URL is named after the sanitized video title.
  Collisions get a `_<short-video-id-hash>` suffix appended — no prompt,
  no failure. (Original spec exited with error; in a GUI app that's
  hostile.)
- Transcripts live in `transcriptions/` like every other project file
  — the existing engine already does this, no special-casing.
- In music mode, what gets transcribed is `vocals.mp3` (not
  `source.mp3`). This is the central insight of the original spec and
  is preserved: cleaner lyrics, fewer hallucinations, a `.srt` that
  pairs naturally with the instrumental.
- `metadata.json` is kept around for an eventual `INDEX.md` builder.

## UI surface

Two additions to the project view (`app/templates/index.html`):

1. **"YouTube" panel** (collapsible, hidden unless `youtube_enabled`):
   - A single text input + "Add URL" button
   - A mode selector next to it: `Speech` / `Music` (defaults to the
     project's `youtube_default_mode`)
   - Below: a list of submitted URLs with status, stage, ETA, and a
     "remove" button for queued/failed rows
2. **Project settings panel** (existing modal) gets:
   - Toggle: "Enable YouTube ingest"
   - Default mode: Speech / Music
   - "Force --no-context for music-mode transcription" toggle
   - "Keep vocals stem after transcribe" toggle

Status of in-flight URL jobs is surfaced through the existing live
status panel — the same one that shows "Chunk 3 of 7" today will
show "Downloading 47%" or "Separating vocals (Demucs htdemucs)".

No new UI tab. No new top-level page. The feature folds into the
existing project-centric flow.

## New API routes

Minimal additions to `app/main.py`. All routes are scoped under a
project, mirroring the existing pattern:

```
POST   /api/projects/<pid>/youtube              { url, mode } -> { id, status }
GET    /api/projects/<pid>/youtube              -> [ { id, url, status, ... } ]
DELETE /api/projects/<pid>/youtube/<job_id>     -> remove from queue (if not in-flight)
POST   /api/projects/<pid>/youtube/<job_id>/retry  -> requeue a failed row
```

The existing `/api/status` payload gains a `current_event.phase` value
of `download` or `separate` when the worker is in those stages.
Existing frontend polling logic is unchanged in shape.

## Dependencies & install

New runtime deps:

- `yt-dlp` — install via `brew install yt-dlp`. Standalone binary,
  self-updates well, scriptable. Add a check to `setup.sh` that warns
  (doesn't hard-fail) if missing — installs lazily on first URL
  submission if absent.
- `demucs` — Python package, installs into the studio's `venv`. Add
  `demucs>=4.0` to `requirements.txt`. **First install pulls PyTorch
  (~2 GB).** This is significant — see Impact section. On Apple
  Silicon, Demucs uses the MPS backend automatically; no extra config.

Both are optional from the studio's perspective:

- Speech mode requires only `yt-dlp`.
- Music mode requires both.
- If the user enables YouTube ingest on a project without the deps
  installed, the settings modal shows a "Run setup" CTA that runs the
  install in a child process and streams output to the UI. No silent
  failures.

We never attempt to install these from inside the launchd-spawned
Python process — same reasoning as the audio-transcribe skill: it
fails opaquely. Always shell out via `Install.command` or surface the
brew/pip commands for the user to run.

## Impact on existing features

This is the section to read twice.

**Worker latency under URL load.** A music-mode URL triggers Demucs,
which can run 5–15 minutes on a 4-minute song on an M-series Mac.
During that time, no other file in any project is transcribing.
For Samvaad-style passive backfill (long queue, runs overnight), a
single song's worth of Demucs is invisible. For someone interactively
re-transcribing a therapy session while also pasting karaoke URLs,
it'll feel like a stall. Mitigation in v1: the live status panel
clearly attributes the block ("Separating vocals — KaraokeLibrary —
3:42 elapsed"). v2 mitigation: split the worker.

**State.json growth.** Per-project `state.json` gains a list that can
grow over time. Not a real problem (URLs are small JSON) but worth
noting: a project that ingests 1000 URLs over a year will have ~200KB
of `youtube_urls` history. Add a "clear completed YouTube history"
button in project settings.

**Disk usage.** Each URL adds ~10MB (speech) or ~30MB (music: source +
instrumental + vocals at 320kbps). The studio's existing logic doesn't
clean these up. The original spec's `--out` flag (point at an external
drive) carries over as the `youtube_subdir` field — projects whose
target folder is on an external drive (Samvaad's already does this)
automatically benefit.

**Hallucination detection still runs.** The existing
`detect_hallucinations()` looks at the produced `.txt` regardless of
source. For music-mode `vocals.txt`, you'll likely see more warnings
than for clean speech — that's actually informative ("this song's
vocals didn't separate cleanly"), not a false positive to suppress.
The UI's existing "Re-transcribe with --no-context" CTA is still the
right next step.

**Scanner exclude patterns.** A project's `exclude_patterns` defaults
won't exclude the `youtube/` subdir, which is what we want — the
files there should be picked up by the scanner. Edge case: if a user
has a glob like `*` in exclude, they'll exclude their YouTube files
too. We'll add a note in the settings UI: "exclude patterns apply to
filenames, not paths."

**Engine flags interaction.** `WhisperConfig.translate_to_english`
(`-tr`) is preserved per-project. For lyrics, you usually do *not*
want translation. We don't override automatically — if your karaoke
project is set to translate, your `.srt` will contain English
translations, which is probably wrong for sing-along. Settings UI gets
a per-project warning when both `youtube_enabled=true`,
`youtube_default_mode=music`, and `translate_to_english=true` —
"This will translate lyrics to English; turn off translation if you
want original-language lyrics for sing-along."

**Conditions engine.** `require_ac_power` and `required_volumes` apply
to URL jobs identically — the worker won't download or separate on
battery if the project demands AC. Good default for Demucs (CPU/GPU
heavy).

**Existing transcribe.sh / bin/transcribe-engine.sh.** Unchanged; not
on the live path. The original `yt-tool.sh` plan is dropped in favor
of `app/yt_ingest.py` — a Python module the worker imports. No shell
entry point.

**Launch agent / Full Disk Access.** No change. The new code runs
inside the same Python process that already has FDA granted (or
doesn't). yt-dlp and demucs use stdlib filesystem APIs that respect
the parent's TCC.

**Hallucination warning banner UI.** Already shows for completed
transcripts. For URL-sourced transcripts, the banner shows up
identically. No special-casing needed.

## Defaults

- Whisper model: inherited from the project's `WhisperConfig`. For
  music mode we recommend (but don't enforce) a multilingual model
  (`large-v3` or `medium`) since song language often differs from a
  user's primary project language. Doc'd in settings, not coded as an
  override.
- `--no-context` for music: **forced on**, controllable via
  `music_force_no_context`. Strong opinion: don't change this unless
  you have a specific reason.
- Demucs model: default `htdemucs` (the package default). No
  configuration in v1.
- Audio format: mp3, 320 kbps (yt-dlp `--audio-quality 0`). Lossless
  isn't worth the disk cost for whisper input.
- Sample rate / mono: whisper preprocessing already enforces 16kHz
  mono via ffmpeg before invoking whisper-cli — same path, no change.
- File naming: `source.mp3` for full audio; `vocals.mp3` /
  `instrumental.mp3` for stems. Transcripts named after the source
  file (`source.txt` or `vocals.txt`).

## Error handling

| Failure                                       | What happens                                                                                |
|-----------------------------------------------|---------------------------------------------------------------------------------------------|
| yt-dlp: geo-block, private, age-gate, removed | URL row → `failed`, `failed_reason` = yt-dlp stderr. UI surfaces it. No retry.               |
| yt-dlp: transient network                     | URL row → `failed` with a "retry suggested" hint. UI shows a "Retry" button. No auto-retry. |
| Demucs: OOM on long track                     | URL row → `failed`, hint: "Re-run with demucs_segment=7 in project settings."               |
| Demucs: missing binary                        | URL row → `failed`, hint linking to install instructions.                                   |
| Whisper: existing failure modes               | Unchanged. URL row reflects the file's transcribe status.                                   |
| Folder collision                              | Sanitized title + `_<video_id_hash[:6]>` appended. Silent.                                  |
| No internet                                   | yt-dlp's error message passes through. URL row → `failed`.                                  |
| User removes URL mid-flight                   | If `status=queued`, just delete. If in-flight, mark for cancel; worker checks between stages and stops cleanly. Partial files stay on disk for the user to inspect. |

The single principle: errors surface in the UI with their original
underlying message. No swallowing, no creative reinterpretation.

## Test cases for v1

Validation runs once the build is wired up — same five from the
original spec, plus three new ones that test studio integration:

Original five (still applicable):

1. **60-min spoken podcast.** Speech mode. Verify the transcript looks
   like other Samvaad outputs; runtime is dominated by whisper, not
   ingest. Should be `large-v3 + medium` neighborhood (~15-20 min on
   medium model).
2. **Short Hindi/Hinglish clip.** Verify project's language flag
   propagates; multilingual model gets used.
3. **English pop song.** Music mode. Verify `vocals.txt` is a
   recognizable lyric sheet and `.srt` timestamps loosely align.
4. **Bollywood song w/ heavy reverb.** Stress test for Demucs.
   Document the quality floor — this becomes the "v1 known limitation"
   example.
5. **YouTube "instrumental" version of a known song.** Skip Demucs,
   transcribe just the source. Compare with the demucs-produced
   instrumental from the original song.

New studio-integration tests:

6. **Submit URL while a file is mid-transcribe.** Verify the URL job
   queues correctly behind the running file, then runs.
7. **Submit URL on battery, project requires AC.** Verify URL stays
   `queued`, status panel reads "waiting for AC power."
8. **Submit same URL twice.** Verify the second submission either
   no-ops (idempotent on URL id) or routes to a new folder via the
   hash suffix — we'll pick one in implementation; document which.

## Open questions to resolve before/during build

Three of the original five were resolved in the Gate 0 review and
moved to the decisions log in `GATE_CHECKIN.md` (entries 9–11). What
remains:

1. **History retention** — auto-prune `youtube_urls` history after N
   days/items? Lean: no, leave it; add a "clear completed" button.
2. **Karaoke-video v2** — once v1 lands, the natural next step is the
   "instrumental + .srt → .mp4 with burned subs" pass. Two-day add-on.

### Resolved (now firm, see GATE_CHECKIN.md)

- **No standalone CLI.** Ingest is a Python module
  (`app/yt_ingest.py`) used only by the studio worker.
- **Re-download on cross-project re-submission.** Each project owns
  its own copy. Symlinks rejected as too fragile.
- **MCP exposure is in scope** for the MCP server track. When the
  routes from `AGENT_APPROACH_BRAINSTORM.md` get exposed as MCP
  tools, `add_youtube_url(project_id, url, mode)` ships with them.
  This feature's v1 doesn't build the MCP server itself — it just
  ensures the underlying HTTP route is shaped so MCP can wrap it
  trivially (clean JSON in/out, stable parameter names).

## Future enhancements (deliberately not v1)

Same as the original spec, lightly re-cast:

1. Playlist ingest — one URL, N rows in `youtube_urls`.
2. Karaoke video output (ffmpeg burns the `.srt` over the
   instrumental).
3. Auto-detect speech vs music from the audio itself.
4. Forced-aligner pass for word-level lyric timing.
5. INDEX.md generator per project that lists every ingested URL with
   title + 1-line summary (pairs naturally with the agent layer in
   `AGENT_APPROACH_BRAINSTORM.md`).
6. yt-dlp cookie file support for age-gated content.
7. Multi-folder routing within a project (currently always lands in
   `folders[0]`).
