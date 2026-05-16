# Transcribe Studio — External Integration Contract

**Status:** v1 design (May 2026). HTTP-only, localhost-only. MCP and auth are explicitly v2.

**Audience:** authors of any external tool that wants to push a YouTube URL into Transcribe Studio and get a transcript back. The first such tool is [Information Studio](file:///Users/ruchir/Documents/cowork-tools/information-studio/) (sibling app digesting <25 YouTube channels). This document also serves as the stable contract for any future caller.

**What this doc is:** a spec for the surface (current routes + the small set of additions needed). It is *not* an implementation — the additions in §3 and §4 are proposed, not yet built. Sections marked **[exists today]** vs **[proposed]** distinguish the two.

---

## §1. Current state — how YouTube ingest works today

Transcribe Studio runs as a localhost Flask app on `127.0.0.1:5180` (long-lived launchd agent). YouTube ingest is wired into per-project pages — a user pastes a URL into the UI card, the worker thread downloads + transcribes, files land in the project's folder.

### Sequence (end-to-end)

```
User clicks "Add URL" in project page
  → POST /api/projects/<pid>/youtube                              [app/main.py:441–479]
     · validates url + mode (speech|music)                        [main.py:459–474]
     · state.add_url(url, mode)                                   [app/projects.py — ProjectState.add_url]
       · generates 12-char hex job id  (uuid.uuid4().hex[:12])
       · appends row to state.youtube_urls
       · persists to data/projects/<id>/state.json
       · row.status = "queued"
     · worker.wake()                                              [app/transcriber.py]
     · returns 201 {"ok": true, "url_row": {...}}

  → Worker thread (serial, one job at a time across all projects)
     · _process_url_job(...)                                      [app/transcriber.py:264]
     · status="downloading"  → YtIngest                           [app/yt_ingest.py]
        · yt-dlp -x --audio-format mp3 → source.mp3
        · landing dir: <project_folder>/<youtube_subdir>/<sanitized_title>[_<6hash>]/   [yt_ingest.py:105–115]
        · writes metadata.json (title, uploader, duration, …)
     · status="separating" (music mode only)
        · demucs --two-stems=vocals → vocals.mp3 + instrumental.mp3
     · status="transcribing"
        · target file = source.mp3 (speech) | vocals.mp3 (music)
        · engine.transcribe() writes <basename>.txt and <basename>.srt
          next to the target audio file
     · status="done"  (or "failed" + failed_reason)

  → Browser UI polls /api/status every 2s, reads state.youtube_urls
```

### Output artifacts (per URL)

Inside `<project_folder>/<youtube_subdir>/<title>_<hash>/`:

| File | When | Source |
|---|---|---|
| `metadata.json` | always | yt-dlp JSON dump (trimmed) |
| `source.mp3` | always | yt-dlp audio extraction |
| `vocals.mp3` | music mode | Demucs separation |
| `instrumental.mp3` | music mode | Demucs separation |
| `<basename>.txt` | after transcription | whisper.cpp + post-processing |
| `<basename>.srt` | after transcription | whisper.cpp |
| `<basename>.txt.analysis.json` | optional, if Ollama analysis enabled | `app/analyzer.py` |
| `<basename>.narrative.<style>.md` | optional, if narrative layer used | `app/narrate.py` |

`<basename>` matches the audio file's stem — i.e., `source.txt` for speech, `vocals.txt` for music.

### Existing surface (project-scoped) — **[exists today]**

All routes return JSON. The comment block at `app/main.py:425–427` declares an explicit invariant: these routes are kept parameter-stable so an MCP server track can lift them later. Treat them as a stable contract.

| Method | Path | Purpose | Source |
|---|---|---|---|
| `POST` | `/api/projects/<pid>/youtube` | Enqueue a URL. Body: `{url, mode?}`. Returns `{ok, url_row}` with 201. | `main.py:441` |
| `GET` | `/api/projects/<pid>/youtube` | List all rows (queued + in-flight + terminal). | `main.py:433` |
| `DELETE` | `/api/projects/<pid>/youtube/<url_id>` | Cancel a queued row. 409 if in-flight. | `main.py:481` |
| `POST` | `/api/projects/<pid>/youtube/<url_id>/prioritize` | Reorder queue. Only valid for `queued` rows. | `main.py:502` |
| `POST` | `/api/projects/<pid>/youtube/<url_id>/retry` | Re-queue a `failed` row. | `main.py:529` |

### Source-of-truth tuples — **[exists today]**

These are defined once in `app/main.py:429–431` and should be the only place a consumer reads the legal values from:

- `_ALLOWED_MODES = ("speech", "music")`
- `_IN_FLIGHT_URL_STATUSES = ("downloading", "separating", "transcribing")`
- `_TERMINAL_URL_STATUSES = ("done", "failed")`
- Implicit: `("queued",)` is the only pre-flight status.

### Integration seam — where to cut

The natural seam is the POST handler at `main.py:441–479`. It already enforces every invariant external callers need: URL shape, mode whitelist, project preconditions, project existence. The proposed `/api/ingest/*` alias in §3 is a thin wrapper around this same handler — it does not duplicate validation.

The one real gap: **today nothing returns the final transcript path** on the row. The UI infers it from filesystem conventions. Closing that gap is the only meaningfully new piece of state in the proposed contract.

---

## §2. Surface options — three candidates, recommendation

Three candidate shapes were considered. Recommendation first; rationale below.

### Recommendation: HTTP REST (option A)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. HTTP REST** (ingest alias over existing project routes) | Exists in skeletal form today; JSON-clean by design; localhost-friendly; Information Studio already prefers it; trivial polling; MCP can wrap it later | Polling not push (fine at our scale); no streaming responses | **Recommended** |
| **B. MCP server** (`ingest_youtube_url`, `get_transcription_status`, `get_transcript` tools) | Clean shape if a Claude agent — not Info Studio — were the caller | Pure overhead for Python↔Python; would wrap the same handlers anyway; Information Studio's research explicitly rejects MCP for v1 ([RESEARCH.md §2.3](file:///Users/ruchir/Documents/cowork-tools/information-studio/RESEARCH.md)) | **Defer to v2.** Revisit when a non-Python or LLM-agent caller appears. |
| **C. File-drop watcher** (caller writes manifest files into a watched directory) | Decouples processes; survives Studio restarts via filesystem | Reinvents the existing queue; loses synchronous validation; loses HTTP error semantics; debugging is terrible | **Reject.** |

### Cross-cutting tradeoffs

- **Auth.** Localhost-only on `127.0.0.1:5180`. No tokens for v1. v2 sketch: optional `X-Studio-Token` header read from a `STUDIO_TOKEN` env var; the header name is reserved here so it isn't a breaking change later.
- **Async vs sync.** All jobs are async — POST returns a `job_id` immediately; caller polls `GET /api/ingest/<job_id>` until terminal. No webhook in v1 (premature; localhost polling is cheap). A `callback_url` field is reserved in the POST schema (currently rejected with `unsupported_field`) so it can be added without breaking the contract.
- **Status communication.** Polling, 2–5s interval recommended. No SSE / WebSocket in v1.
- **Error semantics.** Uniform JSON: `{"ok": false, "error": "<stable_slug>", "message": "<human>", "details": {...optional...}}`. Slug catalog is documented in §3.

---

## §3. Recommended contract — concrete spec

**Base URL:** `http://127.0.0.1:5180` (port configurable via `STUDIO_PORT` env var; default 5180).

The contract has two layers:

1. **Project-scoped routes** — the existing surface listed in §1. Canonical, stable, used by the UI and available to any caller that wants explicit project control.
2. **Ingest-scoped alias** — a thin layer for external callers who don't want to learn about projects. Auto-routes to a configured "default ingest project."

Both layers exist deliberately. Information Studio uses layer 2; advanced callers can drop down to layer 1.

### 3.1 Health probe — **[proposed]**

```
GET /api/health
  → 200 {
      "ok": true,
      "version": "<git_sha>",        ← from app/version.py
      "queue_depth": 7,              ← total queued+in-flight rows across all projects
      "worker_state": "idle" | "busy" | "paused"
    }
```

Used for: liveness ("is Studio running?"), capacity awareness (caller self-throttle), version pinning ("did Studio update underneath me?").

No 5xx body is specified — if `/api/health` returns anything other than 200 JSON, treat Studio as down.

### 3.2 Submit a YouTube URL — **[proposed]**

```
POST /api/ingest/youtube
Body: {
  "url":      "<youtube url>",        REQUIRED
  "mode":     "speech" | "music",     OPTIONAL, default "speech"
  "metadata": { ... }                 OPTIONAL, opaque dict, stored verbatim on the row
}

→ 201 (new row created)
{
  "ok": true,
  "job_id":       "<12hex>",
  "status":       "queued",
  "project_id":   "<pid>",          ← the auto-routed default ingest project
  "submitted_at": "2026-05-16T…",
  "deduped":      false
}

→ 200 (idempotent hit — URL already in queue or done in the default project, non-failed)
{
  "ok": true,
  "job_id":     "<existing 12hex>",
  "status":     "queued" | "downloading" | "separating" | "transcribing" | "done",
  "project_id": "<pid>",
  "deduped":    true
}
```

**Mode parameter.** `speech` (default) is right for spoken-word content — podcasts, talks, lectures, vlogs, anything where the audio *is* the words. `music` is for content where vocals need to be separated from instrumentation before transcription; it adds a Demucs step. Information Studio's fallback ladder uses `speech` for all videos.

**`metadata` field.** An opaque dict stored on the URL row. Useful for callers to tag rows with their own correlation IDs (e.g. `{"caller": "information-studio", "source_channel_id": "UC...", "channel_video_index": 47}`). Studio does not interpret these fields.

**Idempotency (dedupe by URL).** If the same URL has already been submitted to the default ingest project and the existing row's status is not `failed`, Studio returns 200 (not 201) with the existing `job_id` and `deduped: true`. This avoids redundant downloads and Whisper passes. If the existing row is `failed`, a *new* row is created — allowing resubmit-as-retry. Idempotency is scoped per-project, so an advanced caller using the project-scoped POST gets the same dedupe behavior within its chosen project.

**Errors (4xx, stable slugs):**

| Slug | Cause |
|---|---|
| `missing_url` | Body had no `url` field |
| `invalid_url` | URL doesn't start with `http://` or `https://` |
| `invalid_mode` | `mode` not in `("speech", "music")` |
| `youtube_disabled` | Default ingest project has `youtube_enabled=false` |
| `no_folders` | Default ingest project has no folders configured |
| `unsupported_field` | Reserved field name (e.g. `callback_url`) is present |

**Errors (5xx):** `internal_error` — Studio caught an unexpected exception; the `details` field carries a request ID for log correlation.

### 3.3 Read job status — **[proposed]**

```
GET /api/ingest/<job_id>

→ 200
{
  "job_id":        "...",
  "url":           "...",
  "mode":          "speech" | "music",
  "status":        "queued" | "downloading" | "separating" | "transcribing" | "done" | "failed",
  "stage":         "...",                ← finer-grained progress within current status
  "submitted_at":  "...",
  "started_at":    "..." | null,
  "finished_at":   "..." | null,
  "failed_reason": "..." | null,
  "metadata":      { ... },              ← whatever the caller submitted
  "artifacts": {                          ← populated as the job progresses; all keys nullable
    "folder":          "/abs/path/to/<title>_<hash>",
    "audio_path":      "/abs/.../source.mp3" | "/abs/.../vocals.mp3",
    "transcript_path": "/abs/.../source.txt" | null,
    "metadata_path":   "/abs/.../metadata.json"
  }
}

→ 404 if job_id is unknown
```

`artifacts.transcript_path` is the **one new piece of state** introduced by this contract. Today the UI infers transcript paths from filesystem conventions; the new ingest blueprint records it explicitly on the row when transcription completes, so callers don't need to know naming conventions.

### 3.4 Retrieve transcript content — **[proposed]**

```
GET /api/ingest/<job_id>/transcript?format=txt|srt|json   (default: txt)

→ 200 with content-type matching the format
  · txt:  text/plain      raw transcript
  · srt:  application/x-subrip  timestamped subtitles
  · json: application/json {
            "text":     "...full transcript...",
            "segments": [{"start": 0.0, "end": 2.5, "text": "..."}, …],
            "metadata": { …whisper invocation params, audio duration, etc… }
          }

→ 404 if job_id unknown
→ 409 with body {"ok": false, "error": "not_ready", "status": "<current>"} if job not yet done
→ 409 with body {"ok": false, "error": "transcription_failed",
                 "failed_reason": "..."}              if job is failed
```

Both `artifacts.transcript_path` and this content endpoint exist so callers can pick whichever fits — local callers (Information Studio runs on the same machine) will typically read from disk directly via `transcript_path`; future remote/sandboxed callers can stream via this endpoint without filesystem coupling.

### 3.5 Cancel / retry — **[proposed]**

```
DELETE /api/ingest/<job_id>
  → 200 {"ok": true} on success
  → 409 if status is in _IN_FLIGHT_URL_STATUSES   (matches main.py:490–498)

POST /api/ingest/<job_id>/retry
  → 200 {"ok": true, "url_row": {...}}            (only for failed rows; matches main.py:529–551)
  → 409 if status is anything other than "failed"
```

These mirror the existing project-scoped routes 1:1; the alias just lets callers skip the `<pid>` lookup.

### 3.6 Job lifecycle

```
queued ──► downloading ──► [separating]* ──► transcribing ──► done
                                                          ╲
                                                           ╲► failed (+ failed_reason)
```

`*separating` only fires for `mode=music`. The status strings are stable; consumers should treat any unknown value as an unsupported future addition (forward-compatible).

### 3.7 Concurrency

Studio's worker is intentionally **serial** — one job at a time across all projects (`app/transcriber.py`, single Worker thread). This is by design: Whisper saturates CPU, and running two transcriptions in parallel just halves the speed of each.

**If a caller submits 50 URLs at once:**

- All 50 POSTs succeed quickly (just enqueue, no rate-limiting on submit).
- They process in FIFO order. The existing `prioritize` endpoint can reorder queued rows if needed.
- Caller is responsible for any rate-shaping it wants on its side.
- The `queue_depth` field on `/api/health` lets callers self-throttle.
- **No concurrency limits. No async parallelism. No auto-rejection of bulk submissions.** Studio absorbs the queue.

A long-running job (e.g., a 2-hour podcast) will block other queued jobs from making progress. Callers waiting on a fallback transcript should choose their timeout budget accordingly (see §5).

### 3.8 Error slug catalog

Beyond the submit-time errors in §3.2, terminal-row `failed_reason` strings include the following stable categories. The literal text varies (it includes specifics from yt-dlp / Demucs / Whisper output), but consumers can pattern-match on these substrings:

| Category | Typical substring | Cause |
|---|---|---|
| `youtube_unavailable` | `"video unavailable"`, `"private video"`, `"geo-restricted"` | yt-dlp 4xx |
| `download_failed` | `"yt-dlp"`, `"http error"` | Network or transient YouTube error |
| `separation_failed` | `"demucs"` | Music-mode-only failure |
| `transcription_failed` | `"whisper"`, `"engine"` | Engine crash, model missing |
| `disk_full` | `"no space left on device"` | Filesystem full |
| `studio_paused` | n/a (worker paused, row stays queued) | UI or operator paused the worker |

Callers should treat unknown `failed_reason` as `unknown_failure` and report it verbatim to the operator.

---

## §4. Studio-side changes needed to support this contract

**Scope estimate: S** — about a day's focused work.

### 4.1 New files

- **`app/ingest_api.py`** — new Flask blueprint (or routes section in `main.py`) hosting `/api/health`, `/api/ingest/youtube`, `/api/ingest/<job_id>`, `/api/ingest/<job_id>/transcript`, `/api/ingest/<job_id>/retry`, `/api/ingest/<job_id>` (DELETE). Wraps the existing project-scoped handlers; does not re-implement validation. (~150 LOC.)

### 4.2 Touched files

- **`app/main.py`** — register the new blueprint; add `/api/health` route. (~15 LOC.)
- **`app/projects.py`** — three small additions:
  - `default_ingest_project` field on the top-level registry config (with falls-back-to-auto-create semantics).
  - `idempotent=True` flag on `ProjectState.add_url(url, mode, idempotent=False)`. When set, scans existing rows and returns the existing row if found with non-`failed` status.
  - Helper `find_url_by_url(url)` on `ProjectState`. (~30 LOC.)
- **`app/transcriber.py`** — surface the final transcript path on the URL row when transcription completes. A single `state.update_url(url_id, transcript_path=str(target_with_transcript))` call inside the success branch of `_process_url_job` ([`app/transcriber.py:264`](app/transcriber.py)). The path matches the existing `<basename>.txt` convention from `engine.transcribe()`. (~10 LOC.)

### 4.3 Reused, no new infra

- **Queue:** existing single-threaded Worker handles it; no changes.
- **Storage:** existing `data/projects/<id>/state.json`; one new `transcript_path` field on URL rows. Optional one-shot migration on startup to populate the field for existing `done` rows by checking the conventional path on disk.
- **Validation:** existing checks in `api_youtube_submit` are wrapped, not duplicated.
- **Logging:** existing `_log()` to `logs/studio.log`.
- **Cancel / retry / prioritize:** alias routes proxy directly to the project-scoped handlers.

### 4.4 Default ingest project resolution

On a POST to `/api/ingest/youtube`, Studio resolves the target project in this order:

1. If `registry.config.default_ingest_project` is set and the referenced project exists and has `youtube_enabled=true` and at least one folder configured → use it.
2. Otherwise, look up a project named `external-ingest`. If it exists and is usable → use it; set `default_ingest_project` to its ID for next time.
3. Otherwise, auto-create a project named `external-ingest` with:
   - `youtube_enabled=true`
   - Default folder: `~/Documents/Transcribe Studio Ingest/` (created if missing)
   - `youtube_default_mode="speech"`
   - Mark it as the default ingest project in the registry config.

This makes first-call zero-config for the caller and gives users an obvious "this folder is where external tools dump things" location they can move or repoint later via the regular project settings UI.

### 4.5 Idempotency implementation

`ProjectState.add_url(url, mode, idempotent=False)`:

```
if idempotent:
    existing = self.find_url_by_url(url)
    if existing and existing["status"] != "failed":
        return existing, /* deduped= */ True
new_row = <create as today>
return new_row, /* deduped= */ False
```

The `/api/ingest/youtube` handler passes `idempotent=True`; the existing UI-driven `/api/projects/<pid>/youtube` POST passes `False` (preserves current UI behavior — submitting the same URL twice from the UI creates two rows, which power users sometimes use to re-run failed jobs side-by-side).

### 4.6 Auth model

**v1: localhost-only.** Studio already binds to `127.0.0.1` by default. No tokens.

**v2 sketch (not implemented, name reserved):**
- Optional `STUDIO_TOKEN` env var on Studio startup.
- When set, all `/api/ingest/*` routes require header `X-Studio-Token: <value>`. Missing or mismatched header → 401 `{"ok": false, "error": "unauthorized"}`.
- Project-scoped routes (UI traffic) stay token-free for now to avoid breaking the browser UI.

---

## §5. Information Studio's expected calling pattern

Information Studio uses Transcribe Studio as the third tier of a fallback ladder ([RESEARCH.md §1, §2.5](file:///Users/ruchir/Documents/cowork-tools/information-studio/RESEARCH.md)). Tiers 1 and 2 are free YouTube native paths; Studio is the fallback when those fail.

```python
# information-studio worker — pseudocode
def get_transcript(video_url, video_duration_seconds):
    # Tier 1: youtube-transcript-api (free, fast)
    try:
        return youtube_transcript_api.fetch(video_url)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable, RateLimited):
        pass

    # Tier 2: yt-dlp --write-auto-subs (free, slower)
    vtt = run_yt_dlp_write_auto_subs(video_url)
    if vtt:
        return parse_vtt(vtt)

    # Tier 3: Transcribe Studio fallback
    if not studio_healthy():
        return None                          # mark unavailable; revisit later

    job = studio_post("/api/ingest/youtube", {
        "url":  video_url,
        "mode": "speech",
        "metadata": {
            "caller": "information-studio",
            "source_video_id": extract_id(video_url),
        },
    })
    if job["deduped"]:
        log("Studio already had this URL; reusing job %s", job["job_id"])

    deadline = time.time() + fallback_budget_seconds(video_duration_seconds)
    while time.time() < deadline:
        status = studio_get(f"/api/ingest/{job['job_id']}")
        if status["status"] == "done":
            # local-machine optimization: read disk directly
            return read_file(status["artifacts"]["transcript_path"])
            # or, equivalently, for transport-decoupled callers:
            # return studio_get_text(f"/api/ingest/{job['job_id']}/transcript")
        if status["status"] == "failed":
            log("Studio failed: %s", status["failed_reason"])
            return None
        time.sleep(5)

    log("Studio fallback timed out for %s after %s", video_url, deadline)
    return None
```

### 5.1 Caller-side decisions left to Information Studio

| Decision | Recommendation in this doc |
|---|---|
| Port discovery | `STUDIO_URL` env var on caller's side, default `http://127.0.0.1:5180` |
| Offline detection | `GET /api/health` with a short timeout before each batch (not before each individual call) |
| Fallback budget | Heuristic: `min(2 × video_duration, 30min)`. Tune per Info Studio's own design. |
| Bulk submission | Check `queue_depth` from `/api/health`; if > N (caller's threshold), defer rather than pile on |
| Whether to overwrite Studio's transcript on re-submit | Idempotency means Info Studio gets the existing one back — to force re-transcription, DELETE the row first, then POST. Document this gotcha. |
| Cleanup of Studio's artifacts | Info Studio is expected to copy what it needs; Studio retains its own copies (no auto-eviction). Manual cleanup via project UI for now. |

### 5.2 Why these decisions live on the caller side

Studio is the queue, not the policy engine. Timeout heuristics, "should I even attempt fallback for this video," and channel-level dedupe all involve information (video duration, channel metadata, caller's own quota) that Studio doesn't and shouldn't know about.

---

## §6. Non-goals for the first cut

Things this contract **deliberately does not** ship in v1:

- **No MCP server.** Documented as v2; the route shapes are designed so they can be lifted into MCP tools by mechanical wrapping (the comment at `app/main.py:425–427` is the original commitment).
- **No auth.** Localhost-only; v2 problem with `X-Studio-Token` header name reserved.
- **No SSE / WebSocket / webhook callbacks.** Polling is fine at the expected scale. `callback_url` is reserved as an `unsupported_field` for v2.
- **No bulk submission endpoint.** Caller iterates over single POSTs; the queue absorbs.
- **No transcript content caching or compression on Studio's side.** The file on disk is the cache; the content endpoint just reads it.
- **No per-caller rate limiting or quota.** Studio trusts callers on localhost.
- **No automatic cleanup of completed jobs' files.** Studio retains artifacts; callers copy what they need. Cleanup is a v2 endpoint (`DELETE /api/ingest/<job_id>?purge_artifacts=true`).
- **No support for non-YouTube URLs through this endpoint.** Generic-audio ingest is a separate surface if/when needed.
- **No CORS.** Localhost-only, same-machine callers only. If a browser-based caller ever appears, that's its own design conversation.
- **No streaming partial transcripts.** Whisper writes the file when it's done; partial reads from disk aren't supported by the contract (even though Whisper writes incrementally to a temp file in some configurations — that's an implementation detail, not the contract).

---

## Appendix A — Quick reference (one-screen cheat sheet)

```
GET    /api/health                                  liveness + queue depth
POST   /api/ingest/youtube       {url, mode?, …}    enqueue (idempotent)
GET    /api/ingest/<job_id>                         status + artifact paths
GET    /api/ingest/<job_id>/transcript[?format=…]   stream transcript content
DELETE /api/ingest/<job_id>                         cancel (409 if in-flight)
POST   /api/ingest/<job_id>/retry                   re-queue a failed row

  Project-scoped layer (canonical, used by UI; advanced callers can use directly):
GET    /api/projects/<pid>/youtube
POST   /api/projects/<pid>/youtube
DELETE /api/projects/<pid>/youtube/<url_id>
POST   /api/projects/<pid>/youtube/<url_id>/prioritize
POST   /api/projects/<pid>/youtube/<url_id>/retry
```

---

## Appendix B — Source-file pointers

For implementers of either side of the contract:

- `app/main.py:425–479` — existing POST handler + the "keep JSON-clean for MCP" decision comment
- `app/main.py:429–431` — `_ALLOWED_MODES`, `_TERMINAL_URL_STATUSES`, `_IN_FLIGHT_URL_STATUSES` (the source-of-truth tuples)
- `app/main.py:481–551` — DELETE, prioritize, retry handlers
- `app/yt_ingest.py:105–115` — output folder naming convention
- `app/transcriber.py:264` — `_process_url_job` (where the new `transcript_path` field gets set on success)
- `app/projects.py` — `ProjectState.add_url`, `list_urls`, `update_url`, `get_url` (methods the new ingest blueprint calls)
- `SPEC_YOUTUBE_AND_MUSIC.md` — earlier internal spec for the YouTube + Music modes
- `README.md` — Studio's general setup + ops docs
- `/Users/ruchir/Documents/cowork-tools/information-studio/RESEARCH.md` — the caller's perspective (§2.5 in particular)
