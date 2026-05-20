# Transcribe Studio

A local, multi-project transcription studio for your Mac. Wraps whisper.cpp
in a clean Flask UI so you can monitor, pause, prioritize, and view
transcripts across all your projects from a browser tab.

Lives at `~/Documents/cowork-tools/transcribe-studio/`. Runs in the
background via launchd, so the queue keeps moving even when you don't
have the UI open. Open the UI any time at **http://127.0.0.1:5180**.

## What it does

- Register multiple **projects** — each is a name + folders + whisper config
- Auto-scans every project's folders for `.mov .mp4 .m4v .mp3 .m4a .wav` etc.
- Runs whisper.cpp at low CPU priority, one file at a time
- Per-project conditions (require AC power, require a specific volume mounted)
- Live status: which file, which chunk, elapsed time, current model + params,
  rolling tail of whisper-cli output
- Pause / resume globally
- Prioritize specific files, skip files, re-transcribe with different settings
- Built-in **hallucination detection** on every completed transcript
- View transcripts in the browser without leaving the app
- **YouTube ingest** (per-project): paste a video URL, studio downloads
  audio via yt-dlp and queues it for transcription. Music mode
  additionally splits vocals/instrumental via Demucs.
- **YouTube playlist → project**: paste a public playlist URL and the
  studio creates a new project with every video queued for ingest.
- **External read access** via `bin/ts` CLI — skills and sibling apps can
  list projects, fetch transcripts, queue URLs without writing HTTP code.
  See `INTEGRATION.md` §7.

## What it does NOT do

- It does not implement whisper itself — it shells out to `whisper-cli` from
  Homebrew. You do that install once.
- It does not move your videos. Transcripts always go in
  `<source-folder>/transcriptions/<basename>.txt` (and `.srt`).

## First-time install

You need (one-time on the Mac):

```bash
brew install whisper-cpp ffmpeg
mkdir -p ~/Documents/cowork-tools/whisper-models
curl -L --fail -o ~/Documents/cowork-tools/whisper-models/ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
curl -L --fail -o ~/Documents/cowork-tools/whisper-models/ggml-silero-v6.2.0.bin \
  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin
```

Then double-click **Install.command**. It:
1. Creates `venv/`, installs Flask
2. Removes macOS quarantine flags from the studio files
3. Installs and starts the launchd agent
4. Removes the old `com.samvaad.transcribe` agent (if present)
5. Opens the studio in your browser

### macOS Full Disk Access

Because launchd-spawned processes don't inherit Terminal's permissions,
you need to grant Full Disk Access **once**:

1. **System Settings → Privacy & Security → Full Disk Access**
2. Click **+**, press **Cmd+Shift+G**, navigate to `/bin/bash`, add it
3. Click **+**, navigate to `~/Documents/cowork-tools/transcribe-studio/Transcribe Studio.app`, add it
4. Make sure both toggles are ON

If you skip this step the worker will fail with `Operation not permitted`
on every file. The studio's status panel surfaces this.

## Day-to-day

- **Open studio**: double-click `Transcribe Studio.app` or `Open Studio.command`
  (or just hit `http://127.0.0.1:5180` in any browser)
- **Pause / resume**: button in the upper-right
- **Add a project**: `+ New project` in the sidebar
- **Add a YouTube playlist as a project**: `+ Add from YouTube playlist`
  in the sidebar → paste the playlist URL → preview → confirm. Creates
  the project at `~/Documents/Transcribe Studio/Playlists/<title>/`.
- **Pick a specific file to transcribe next**: hover the row in the project view, click `↑`
- **Re-transcribe a bad transcript**: hover the row, click `redo`. The studio detects
  hallucination loops automatically and shows a warning banner with the suggested fix.
- **View a transcript**: click any completed row. Shows the `.txt` and a quality assessment.
- **Stop transcribing**: pause via UI, OR run `Uninstall.command` to remove the agent entirely

## Files

- `app/` — Python source
- `bin/transcribe-engine.sh` — original bash engine kept as reference (the Python `engine.py` is the live one)
- `bin/ts` — Python CLI for external consumers (skills, scripts, sibling apps); thin wrapper over the HTTP API
- `INTEGRATION.md` — contract for external callers (read + write, including playlist endpoints)
- `data/projects.json` — project registry
- `data/projects/<id>/state.json` — per-project queue/history
- `logs/studio.log` — main rolling log
- `logs/launchd.{out,err}.log` — launchd capture
- `venv/` — Python venv (created by `setup.sh`)
- `Transcribe Studio.app/` — Mac launcher app, double-click target

## Studio architecture

```
                ┌────────────────────────────────────────────┐
                │  Browser  http://127.0.0.1:5180            │
                │  (HTML/CSS/JS, polls /api/status every 2s) │
                └────────────────┬───────────────────────────┘
                                 │ HTTP
                ┌────────────────▼───────────────────────────┐
                │  Flask app (app/main.py)                   │
                │  Routes: status, projects, queue, log      │
                └────────────────┬───────────────────────────┘
                                 │
                ┌────────────────▼───────────────────────────┐
                │  Worker thread  (app/transcriber.py)       │
                │  loop: pick file → run engine → log result │
                └────────────────┬───────────────────────────┘
                                 │
                ┌────────────────▼───────────────────────────┐
                │  Engine  (app/engine.py)                   │
                │  yields events as it shells out to:        │
                │    ffprobe → ffmpeg → whisper-cli          │
                │  applies VAD + chunking + max-len defaults │
                │  detects hallucinations on completion      │
                └────────────────────────────────────────────┘
```

## Troubleshooting

**"Backend offline" in the UI.** Check `logs/launchd.err.log`. Most likely
either Full Disk Access isn't granted, or `whisper-cli`/`ffmpeg` isn't on
the launchd PATH. Re-run `setup.sh` to verify.

**Files all show "failed".** Open one — the failure reason is logged in
`logs/studio.log`. Common causes: model file missing, drive unmounted,
`Operation not permitted` (= TCC, see Full Disk Access above).

**Transcript looks repetitive / has loops.** Click the row → the quality
banner explains it. Click **Re-transcribe** after enabling
**--no-context** in the project settings (that's the proven fix per the
audio-transcribe skill's notes — disables cross-segment context, which is
what fuels the loop).

**Two whisper-cli's running at once.** Shouldn't happen — the worker is
strictly serial — but if it does, `Uninstall.command` then
`Install.command` resets cleanly.
