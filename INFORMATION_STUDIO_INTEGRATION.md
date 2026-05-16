# Information Studio integration

**Status:** design, pre-build. Last updated 2026-05-16.

**Counterpart doc:** Information Studio's own design lives at
`~/Documents/cowork-tools/information-studio/`. As of this writing, that
folder contains `RESEARCH.md` but **not yet `DESIGN.md`** — a planning task
is in flight to produce it. Numbers and behaviors quoted here against
RESEARCH.md should be cross-checked when DESIGN.md lands and this doc
revised if its choices diverge.

---

## 1. The integration job

Information Studio digests a curated set of YouTube channels into a low-time-cost
information diet. It has a transcript-fetch ladder that tries cheap unofficial
sources first (`youtube-transcript-api`, `yt-dlp --write-auto-subs`) and falls
back to a real transcription pass when those return nothing usable — live
streams, music videos, captions disabled, freshly-uploaded videos whose
auto-captions haven't generated yet, regional blocks on the unofficial caption
endpoint, etc. Transcribe Studio is the producer of that last-resort
transcription: Info Studio submits a YouTube URL, Studio downloads the audio
and runs whisper.cpp, and Info Studio reads the resulting `.txt` from disk and
moves on to summarization. This doc defines the producer-side contract so both
apps can be built against the same shape.

---

## 2. Current capabilities (audit, not proposal)

Everything in this section already exists and is verified against the code on
this branch. No new Studio code is required to support Info Studio's Phase 1.

**Process model.** Single Flask app at `http://127.0.0.1:5180`, launchd-managed,
no auth. One serial worker thread runs one whisper-cli (or one yt-dlp / demucs
stage) at a time across all projects. See `HANDOFF.md` "How it actually runs"
for the launchd / FDA / TCC details.

**YouTube ingest routes** (in `app/main.py:424–551`, intentionally shaped for
external callers per decision #11 in `app/yt_ingest.py:11–14`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/projects/<pid>/youtube` | Submit URL. Body `{"url": str, "mode": "speech"\|"music"}`. Returns 201 `{"ok": true, "url_row": {...}}` |
| `GET` | `/api/projects/<pid>/youtube` | List all rows for a project: `{"urls": [...]}` |
| `DELETE` | `/api/projects/<pid>/youtube/<url_id>` | Cancel. 409 if `status in ("downloading","separating","transcribing")` |
| `POST` | `/api/projects/<pid>/youtube/<url_id>/prioritize` | Move a `queued` row to the front |
| `POST` | `/api/projects/<pid>/youtube/<url_id>/retry` | Requeue a `failed` row |

**Lifecycle states** on a `url_row`:

```
queued → downloading → (separating, music only) → transcribing → done
                                                              └─► failed (any stage)
```

Stage-aware: `url_row.stage` tracks the in-flight phase; `url_row.failed_reason`
preserves the underlying yt-dlp / demucs / whisper stderr. See
`SPEC_YOUTUBE_AND_MUSIC.md` "Status lifecycle" and "Error handling".

**Output layout** (deterministic, `app/yt_ingest.py:105–115` + spec
"Folder layout"):

```
<project.folders[0]>/<project.youtube_subdir, default "youtube">/
└── <sanitized-video-title>[_<6-char-hash>]/
    ├── metadata.json
    ├── source.mp3
    ├── (music mode only) vocals.mp3, instrumental.mp3
    └── transcriptions/
        ├── source.txt
        └── source.srt
```

The 6-char hash is `sha1(video_id)[:6]` and is appended only on folder-name
collision. `target_file` in the `url_row` points at the audio file once known;
the transcript path is always `<dirname(target_file)>/transcriptions/<stem(target_file)>.txt`.

**Transcript read.** `GET /api/projects/<pid>/transcript?path=<absolute_path>`
returns the transcript text plus a quality assessment (`detect_hallucinations`)
that flags loops and low-diversity output. This is what the Studio UI uses to
render the warning banner. Optional for Info Studio; the `.txt` is plain text
on disk and can be read directly.

**Dedup policy** (decision #10, `app/yt_ingest.py:9–11`): the caller owns the
destination folder. Studio does **not** look across projects to dedupe — if you
submit the same URL twice, you get two folder trees (the second with a
`_<hash>` suffix) and two transcribe runs.

**Conditions engine.** AC-power and required-volumes gating apply to URL jobs
identically to file jobs (`SPEC_YOUTUBE_AND_MUSIC.md` "Conditions engine"). If
the project demands AC power and the Mac is on battery, the URL stays `queued`.

**MCP server.** Not built yet. The route shape was deliberately kept
parameter-stable so an MCP wrapper can lift them as-is when that track ships
(`AGENT_APPROACH_BRAINSTORM.md` "How an MCP server fits"). Phase 2 of this
integration.

**Sidecar narrative layer** (`app/narrate.py`, Claude Opus 4.7). Produces
`<stem>.analysis.json` and `<stem>.<style>.narrative.md` next to a transcript
on demand. Not on the Info Studio critical path; available as a richer
secondary surface if Info Studio later wants pre-analysis instead of doing all
summarization itself.

---

## 3. Contract surface — evaluation

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **HTTP API (existing routes)** | Already built and stable; JSON-clean by decision #11; Info Studio's RESEARCH.md §2.5 already walked the exact wrapper sequence; both apps Python so client is ~30 LOC | Polling adds tiny latency; both apps must agree on the on-disk path convention | **Recommended for Phase 1** |
| MCP server | Future-proof — Claude Desktop / Claude Code can call the same tools; aligns with the brainstorm doc's "studio as infrastructure" stance | Not built. Info Studio is Python and would still call the same underlying logic via a different transport; no functional win over HTTP for this consumer | Phase 2, parallel track |
| File-drop / shared manifest | Decouples runtime (Studio reads a watched file) | Reinvents the queue Studio already has; needs new code in Studio; opaque debugging compared to JSON HTTP | Reject |
| CLI subprocess (`yt-tool.sh` etc.) | Trivial to invoke from any language | Decision #9 in `app/yt_ingest.py` explicitly rejects a standalone CLI; loses queue, conditions, hallucination detection, UI visibility | Reject |

**Pick: HTTP API.** Every piece of plumbing Info Studio needs already exists,
decisions #10 and #11 in `yt_ingest.py` were written with exactly this consumer
in mind, and Info Studio's RESEARCH.md §2.5 already chose this surface. Phase 2
adds the MCP transport on top of the same backing logic — when that lands, the
swap inside Info Studio's client module is small.

---

## 4. Request shape

Info Studio submits a fallback transcription job with:

```http
POST /api/projects/<pid>/youtube HTTP/1.1
Host: 127.0.0.1:5180
Content-Type: application/json

{
  "url":  "https://www.youtube.com/watch?v=<id>",
  "mode": "speech"
}
```

- `<pid>` is the slug of the dedicated `info-studio-fallback` project (see §11,
  Locked Decisions). Info Studio creates this project on first run via
  `POST /api/projects` and caches the returned `pid`.
- `mode` is always `"speech"` for Info Studio's use case. Music mode exists
  for human-curated karaoke workflows and is not part of this contract.
- No priority, no callback URL, no channel context, no per-request whisper
  config. The dedicated project's `WhisperConfig` is the only knob and it
  applies to every fallback drop. If Info Studio later needs per-video config,
  it can either flip a project-level setting before submit or create additional
  fallback projects.

**Successful response (201):**

```json
{
  "ok": true,
  "url_row": {
    "id": "<uuid>",
    "url": "https://www.youtube.com/watch?v=<id>",
    "mode": "speech",
    "status": "queued",
    "stage": null,
    "submitted_at": "2026-05-16T10:30:00",
    "title": null,
    "video_id": null,
    "target_file": null,
    "metadata_path": null,
    "failed_reason": null,
    "started_at": null,
    "finished_at": null
  }
}
```

Info Studio stores `url_row.id` indexed by its own video_id and polls from there.

**Error responses** (all return `{"ok": false, "error": <code>, "message": <str>}`):

| HTTP | `error` | Cause |
|---|---|---|
| 400 | `missing_url` | Empty/whitespace `url` |
| 400 | `invalid_url` | Doesn't start with `http://` or `https://` |
| 400 | `invalid_mode` | Not `"speech"` or `"music"` |
| 400 | `youtube_disabled` | Project's `youtube_enabled` is false — Info Studio must set this when creating the fallback project |
| 400 | `no_folders` | Project has no folders configured |
| 404 | — | Unknown `<pid>` |

---

## 5. Response shape — completed job

Once `url_row.status == "done"`, the row contains everything Info Studio needs
to read the transcript. Pulled from `GET /api/projects/<pid>/youtube`:

```json
{
  "id": "<uuid>",
  "url": "https://www.youtube.com/watch?v=<id>",
  "mode": "speech",
  "status": "done",
  "stage": null,
  "title": "How attention works",
  "video_id": "<youtube_id>",
  "target_file": "/Users/.../info-studio-fallback/youtube/How attention works/source.mp3",
  "metadata_path": "/Users/.../info-studio-fallback/youtube/How attention works/metadata.json",
  "submitted_at": "2026-05-16T10:30:00",
  "started_at":   "2026-05-16T10:30:14",
  "finished_at":  "2026-05-16T10:51:02",
  "failed_reason": null
}
```

**Transcript path convention** (derive, don't ask):

```python
target  = Path(url_row["target_file"])        # …/source.mp3
txt     = target.parent / "transcriptions" / f"{target.stem}.txt"
srt     = target.parent / "transcriptions" / f"{target.stem}.srt"
```

This holds for every Studio-produced transcript and is enforced by
`app/engine.py`'s output path. Info Studio reads `txt` directly.

**What's NOT in the row** that Info Studio might want:

- Language: Studio doesn't store the detected language as a field. If needed,
  `metadata.json` includes yt-dlp's `language` if YouTube exposed one; the
  whisper-detected language is not surfaced separately in v1.
- Word-level timestamps: Studio produces segment-level `.srt`. Word timing
  would require a `whisperX` pass — out of scope.
- Hallucination flag: not on the `url_row`. To get it, call
  `GET /api/projects/<pid>/transcript?path=<txt-path>` which returns
  `{"text": "...", "quality": {"hallucinated": bool, "reason": "..."}}`.
  Phase 1 doesn't require this — Info Studio's summarization LLM smooths over
  most artifacts and Studio's `--no-context` default already kills the common
  loop mode.

---

## 6. Async / sync model

**Polling.** Info Studio polls `GET /api/projects/<pid>/youtube` and filters by
`row.id`. Recommended cadence:

- **Default interval:** 30 seconds for fresh submissions
- **Backoff:** if `status == "transcribing"` and `started_at` is older than
  10 minutes, back off to 60 seconds; older than 30 minutes, back off to 120
  seconds. Long video + serial worker = patience.
- **Hard timeout:** Info Studio's call site decides when to give up. The
  90-minute soft cap on submission (Locked Decisions, §11) makes >2-hour
  end-to-end latency unusual but possible on a long queue.

**Why not webhook / callback?** Requires new Studio code, requires Info Studio
to expose a callback URL (or an IPC socket), and adds a coupling that doesn't
pay off until Info Studio runs many concurrent fallbacks. Studio's worker is
serial — Info Studio is naturally rate-limited. Polling is fine.

**Why not SSE / long-poll?** Nothing wrong with it; just no win at this scale
(one HTTP request per minute is free). Phase 2 if useful.

**Wake-on-submit.** `POST .../youtube` calls `worker.wake()` internally
(`app/main.py:478`), so the worker starts the job within ~1s of submission if
it was idle. Info Studio's first poll right after submit will usually find
`status: "downloading"` or later.

---

## 7. Failure modes

| Failure | What Info Studio sees | Recovery |
|---|---|---|
| YouTube video unavailable (geo-block, private, age-gate, removed) | `status: "failed"`, `failed_reason` = yt-dlp stderr. Stage `downloading` | Mark video "transcript unavailable" in Info Studio. No auto-retry — yt-dlp errors here are rarely transient. |
| Transient network during download | `status: "failed"`, `failed_reason` mentions DNS / timeout / 5xx | `POST .../<id>/retry` once. If it fails twice, give up. |
| Whisper failure (model missing, FDA denied, disk full) | `status: "failed"` after stage `transcribing`. `failed_reason` carries the underlying error | Don't auto-retry — it's a Studio host problem, not a per-video problem. Surface to Info Studio's operator. |
| Audio downloaded but transcription hallucinates | `status: "done"`, transcript is repetitive | Info Studio summarization smooths over local errors; if quality matters, call the transcript API and read `quality.hallucinated` |
| Disk full mid-job | `status: "failed"`, error from yt-dlp or whisper-cli depending on stage | Operator problem; surface |
| Studio offline (launchd agent stopped, port closed) | Connection refused on `POST` or `GET` | Info Studio surfaces "fallback unavailable", retries the submission with exponential backoff (5s, 30s, 5m, give up). The rest of Info Studio's ladder already accepts that step 3 can fail. |
| Project `youtube_enabled` got toggled off | 400 `youtube_disabled` on `POST` | Operator must re-enable; Info Studio surfaces the message |
| Same URL submitted twice | Two `url_row` entries, two folders (second has `_<hash>` suffix), two transcribe runs | Info Studio dedupes on its side keyed by video_id before submission — see §9 |
| User cancels mid-flight via Studio UI | Studio worker stops at the next stage boundary, row goes `failed` with a cancel marker | Info Studio retries or marks unavailable |
| Worker pre-empted by AC-power / volume condition | Row stays `queued` indefinitely | Info Studio's poll loop tolerates this; long-stuck `queued` is the operator's signal |

**Single principle, inherited from Studio's existing error handling:** errors
surface with their original underlying message in `failed_reason`. Info Studio
should preserve that string in its own state so the operator can debug without
reading Studio's logs.

---

## 8. Auth / boundary

**Today (both apps on the same Mac):**

- Studio binds to `127.0.0.1:5180` only — kernel-level rejection of off-host
  connections.
- No auth, no token, no CSRF protection. Anyone with shell access on the Mac
  can hit the API.
- The trust model is: "loopback access ≈ shell access ≈ already trusted".
  This is the existing posture for everything on this machine.

**If Info Studio is ever moved off this Mac:**

- The localhost bind has to become an explicit choice — either Studio gets
  bound to a non-loopback interface (don't) or, preferred, Studio stays on
  loopback and a reverse proxy / SSH tunnel terminates Info Studio's traffic.
- Auth has to land before that. Options, cheapest to richest:
  1. A shared secret in a header (Info Studio reads `STUDIO_TOKEN` env, Studio
     verifies on each request). 30 LOC change.
  2. mTLS via the reverse proxy.
  3. OAuth-style flow if multi-user becomes a thing (unlikely for this app).
- Rate-limiting is not in scope today (single user, serial worker is the
  natural limit). Becomes interesting only if Info Studio is multi-tenant.

**Don't ship remote access without auth.** The routes accept arbitrary URLs
and arbitrary project IDs — an unauthenticated remote attacker could fill the
disk with whisper jobs against arbitrary YouTube URLs.

---

## 9. Storage and dedup

**Studio's policy** (decision #10, `app/yt_ingest.py:9–11`): callers own dedup.
A repeated URL produces a second folder tree with a `_<6-char-hash>` suffix
and a second transcribe run.

**Info Studio's responsibility** (matches RESEARCH.md §2.5):

1. **Pre-submit dedup:** Info Studio's own state store (the SQLite mentioned
   in RESEARCH.md §3) keys by `video_id`. Before submitting a fallback, check
   "have we already transcribed this video?" If yes, skip.
2. **Reuse vs re-transcribe across runs:** if Info Studio's state says it
   submitted this video before but the transcript is gone (operator deleted
   the project folder, etc.), submit again. Otherwise read from the cached
   path.
3. **No cross-project reach.** Even if the operator has another Studio project
   that happens to have transcribed the same video, Info Studio doesn't go
   looking. The `info-studio-fallback` project is the single canonical
   location.

**Cleanup.** Studio doesn't auto-evict completed YouTube downloads
(`SPEC_YOUTUBE_AND_MUSIC.md` "Disk usage"). Info Studio can optionally
`DELETE /api/projects/<pid>/youtube/<id>` after slurping the transcript to
prune the row, but the on-disk audio + transcript files stay. A periodic
janitor in Info Studio that deletes folders for videos older than N days is a
reasonable Phase 2 add.

**Suggested project config** for `info-studio-fallback`:

```json
{
  "name": "Information Studio fallback",
  "folders": ["/Users/<user>/Documents/cowork-tools/information-studio/data/transcripts"],
  "youtube_enabled": true,
  "youtube_default_mode": "speech",
  "youtube_subdir": "youtube",
  "auto_run": true,
  "require_ac_power": false,
  "exclude_patterns": [],
  "config": { "model": "large-v3", "no_context": true, "vad": true }
}
```

`require_ac_power=false` is the only non-default. The fallback pipeline should
not wait on a power state — if Info Studio submitted it, it's wanted now.

---

## 10. Build phases

### Phase 1 — MVP

**Goal:** smallest contract that unblocks Info Studio's fallback ladder.

In Info Studio (none of these are Studio changes):

1. Add `STUDIO_URL` env var support, default `http://127.0.0.1:5180`.
2. On first run, ensure the `info-studio-fallback` project exists via
   `POST /api/projects` (idempotent — 409 means already exists, look it up).
   Cache the `pid` in Info Studio's state.
3. Fallback step in the transcript ladder: if duration ≤ 90 min (soft cap,
   configurable), `POST .../youtube` with `mode="speech"`. Save returned
   `url_row.id` keyed by video_id.
4. Poll `GET .../youtube`, filter by `id`, follow the backoff schedule in §6.
5. On `done`: read `<target_file_parent>/transcriptions/source.txt` from disk,
   feed it to summarization, mark the video transcribed in Info Studio's state.
6. On `failed`: store `failed_reason`, retry once on transient-looking errors,
   then mark video transcript-unavailable.

In Studio: **nothing**. The contract is what the code already does.

**Phase 1 explicit non-goals:**

- No MCP server wrap.
- No webhook / callback from Studio.
- No length-cap enforcement inside Studio (cap is Info Studio's concern).
- No automated cleanup of the fallback project folder.
- No cross-app health endpoint — TCP connect failure on `127.0.0.1:5180` is
  sufficient signal.
- No new fields on `url_row` (language, detected speakers, etc.).
- No analysis sidecar in the fallback flow — Info Studio runs its own
  summarization.

### Phase 2 — Richer

The MCP track lands. Studio gets a thin MCP wrapper exposing the same routes
as named tools (`transcribe-studio:add_youtube_url`, `…:list_youtube`, etc.).
Info Studio's client gains an MCP transport behind the same Python interface;
swap is local to one module.

After MCP:

- **Webhook back to Info Studio.** Useful when Info Studio gets enough volume
  that polling feels wasteful. Add an optional `callback_url` field on the
  submit payload; Studio POSTs the completed `url_row` to it.
- **Batch submit.** `POST .../youtube/batch` with an array of URLs. Just sugar
  over the existing single-submit; fewer round trips for backfill.
- **Auth.** Shared-secret header, gated on a Studio config flag. Needed before
  any remote-Info-Studio deployment.
- **Analysis sidecar exposure** via `app/narrate.py` — Info Studio could
  request a pre-analysis JSON instead of running its own summarization pass,
  centralizing the "what does this video say" logic in Studio. Speculative;
  only worth it if Info Studio's summarization prompts converge on shapes
  `narrate.py` already produces.
- **Detected-language field** on `url_row`. Cheap (whisper.cpp prints it);
  worth surfacing once Info Studio cares.

---

## 11. Locked decisions

| # | Decision | Why | Re-open when |
|---|---|---|---|
| 1 | Contract surface is the existing HTTP API on `127.0.0.1:5180`. No new Studio endpoints in Phase 1. | Already built and JSON-clean by decision #11 in `app/yt_ingest.py`; RESEARCH.md §2.5 picked the same path; no other option is cheaper. | MCP server ships and benchmarks suggest meaningful win over HTTP for Info Studio specifically. |
| 2 | Sync model is client-side polling, 30s default with backoff to 60s/120s as a job ages past 10/30 min. | Studio's worker is serial; webhook adds coupling for no current benefit; Info Studio's ladder already accepts step-3 latency. | Info Studio runs concurrent fallbacks or operator complains about polling load. |
| 3 | Info Studio uses a dedicated `info-studio-fallback` Studio project, auto-created on first run via `POST /api/projects`. Single `pid`, single layout. | Stable disk layout, easy cleanup, no per-deploy config friction, no risk of dumping fallback drops into a human's curated project. | An operator asks for multiple fallback projects (e.g. split by channel category). |
| 4 | Info Studio discovers Studio via `STUDIO_URL` env var, default `http://127.0.0.1:5180`. | Trivial override for testing; defaults match Studio's actual bind; no config file ceremony. | A managed-install path needs a more structured config story. |
| 5 | Info Studio applies a soft 90-minute duration cap before falling back, configurable via Info Studio config. Studio does **not** enforce a cap. | Studio's worker is serial — a 4-hour live stream would starve Info Studio's other fallbacks. Caller-side gating keeps Studio dumb. | A specific channel publishes critical-to-digest >90min videos routinely; raise the operator's cap. |
| 6 | Info Studio reads finished transcripts via direct filesystem read of `<dirname(target_file)>/transcriptions/<stem(target_file)>.txt`. The hallucination quality flag is available via `GET /api/projects/<pid>/transcript?path=…` but optional. | Both apps share the disk; the convention is deterministic; one fewer HTTP call per video. Quality flag isn't load-bearing for summarization. | Info Studio wants to surface "we summarized a likely-hallucinated transcript" in its UI. |
| 7 | Info Studio always submits `mode="speech"`. Music mode is human-curated only. | Music mode triggers Demucs (5–15 min per song) and produces vocal stems aimed at karaoke, not info-diet. No useful summarization comes from a music split. | Info Studio adds a music-channel digest feature and validates that vocal-stem transcripts summarize well. |
| 8 | Dedup is Info Studio's responsibility, keyed by YouTube video_id in its own state store. Studio re-downloads on resubmission per decision #10. | Studio is intentionally caller-dumb about dedup; Info Studio already needs a video_id index for its own UX. | Never — this is a Studio-side architectural decision (#10 in `yt_ingest.py`). |
| 9 | No auth in Phase 1; trust model is "loopback ≈ shell access". A shared-secret header gets added before any remote-Info-Studio deployment. | Single-user same-host posture matches the rest of the toolchain. | Info Studio gets moved off this Mac, or multi-tenant becomes real. |
| 10 | Info Studio's `DESIGN.md` did not exist when this was written. This doc references `RESEARCH.md` and must be revisited once `DESIGN.md` lands. | The producer-side contract can't be finalized in isolation from the consumer's design — this is a snapshot of current alignment. | `~/Documents/cowork-tools/information-studio/DESIGN.md` is committed. |
