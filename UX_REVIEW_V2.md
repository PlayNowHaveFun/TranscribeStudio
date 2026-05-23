# Transcribe Studio — UX Review & Revamp Proposal (v2)

Owner: Ruchir
Drafted: 2026-05-16
Status: review — not a build doc

This is the kickoff design doc for v2. The product has evolved well past
"Samvaad backfill tool" — it now handles multi-project queues, YouTube
ingest (speech + music), Ollama analysis sidecars, Claude Opus narrative
generation, hallucination detection, periodic folder scanning, and is
about to become Information Studio's fallback transcriber. The UI has
accreted feature-by-feature; some pieces landed cleanly, some are
grafted on, some are only discoverable if you already knew they existed.

This doc audits where the UX has lagged, names the pain points, and
proposes a ranked set of changes for v2. **No code in this PR.** A
separate worktree will execute the changes after this review is signed
off.

The user has explicitly locked three things and asked that this review
not propose changing them: the flow/navigation pattern, the color
palette, and the project model. They're called out as locked in §2 and
§5.

---

## 1. Current state audit

What's actually on screen today, with file:line refs for every claim.

### 1.1 Screens

| Screen | Rendered by | Contents |
|---|---|---|
| **Dashboard** (default) | `renderDashboardStructure()` `app/static/app.js:158–189` | Overview metric tiles (Done / Pending / Failed / Skipped / Complete %); "Now playing" card; project-card grid |
| **Project view** | `renderProjectStructure()` `app/static/app.js:272–338` | Project header (name + folders), Progress card + settings chips (model, language, translate, VAD, ordering, auto-run, YouTube status), "Now playing", YouTube panel (if enabled), Files card with 3 sub-tabs (**Local Files / YouTube / Music**) |
| **Transcript modal** | `app/templates/index.html:129–176` | Three tabs: **Transcript** (txt + quality banner), **Analysis** (Ollama summary + topic pills + meta), **Narrative** (Claude Opus 4.7, style dropdown, scaffold pane, markdown body) |
| **New-project modal** | `app/templates/index.html:42–126` | Name + folders + model + language + translate/VAD/auto-run, `<details>Advanced</details>` (no-context, AC power, **YouTube ingest**, volumes, excludes), `<details>AI Analysis (Ollama)</details>` |

### 1.2 Design tokens (`app/static/style.css:6–58`)

Dark theme:

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0d0d10` | page background |
| `--surface` / `--surface-2` / `--surface-3` | `#13131a` / `#1a1a24` / `#21212e` | nested card depths |
| `--border` / `--border-strong` | `#2a2a3a` / `#3a3a50` | dividers, focus |
| `--text` / `--text-muted` / `--text-faint` | `#e8e8f0` / `#8888a8` / `#55556a` | primary / secondary / tertiary text |
| `--accent` | `#7c3aed` | primary CTA (purple) |
| `--success` | `#34d399` | done states |
| `--warning` | `#fbbf24` | warnings (VAD off, hallucination) |
| `--danger` | `#f87171` | failures, errors |

Hierarchy is clean and the contrast holds up; nothing to revisit here.

### 1.3 Empty / loading / error states catalogue

| State | Where | Treatment today |
|---|---|---|
| No projects | `app.js:223` | "No projects yet. Click ＋ New project on the left to add one." (one sentence) |
| Transcript loading | `app.js:986` | "Loading…" |
| Transcript pending | `app.js:1034` | "(this file hasn't been transcribed yet — close this and use ↑ to prioritize it)" |
| No analysis | `app.js:1220–1226` | Centered button: "✦ Analyze with qwen2.5-coder" |
| No narrative | `app.js:1080` (`index.html:165–168`) | "No narrative yet for this style. Click Generate to send the transcript to Claude Opus 4.7. Requires `ANTHROPIC_API_KEY` exported in the environment." |
| No YouTube URLs | `app.js:834` | "No URLs yet — paste one above to ingest." |
| Files filter no-match | `app.js:595` | "No files match." |
| Ollama not running | `app.js:1307` (new-project modal only) | "Ollama not running — start it to enable analysis" |
| Backend offline | `app.js:61` | Sidebar status pill flips to "backend offline" — no actionable recovery hint |
| Worker paused | progress card | Badge says "Paused" — no reason chip (AC? volume? user?) |

### 1.4 Discoverability inventory

- **Row actions are hover-only.** ↑ prioritize, `redo`, `view` live in `.actions` revealed only on row hover (`app.js:651–657`, `app.js:699–701`).
- **Narrative + Analysis tabs.** Only discoverable by opening a completed transcript and noticing the tabs (`index.html:137–141`).
- **YouTube ingest opt-in.** Checkbox tucked inside `<details>Advanced</details>` in the new-project modal (`index.html:88`).
- **Browse folder picker.** Small "Browse…" button next to the folders textarea (`index.html:54`). Recent addition, underplayed.
- **Priority queue.** User clicks ↑, file moves to `state.priority_queue`, list shows no ordering indicator. The feature is effectively invisible.
- **Music tab vs YouTube tab.** Files card has three sibling tabs — but Music is a *mode* of YouTube, not a separate source. New users have no way to know that.
- **Pause reason.** Worker can be paused for AC power, missing volume, or manual pause. The badge alone doesn't say which.
- **Periodic scan.** `watcher.py` re-scans every 5 minutes. The UI never tells you a scan happened.

---

## 2. What's working (locked-good — preserve in v2)

These are explicitly off-limits for v2 changes. Calling them out here so
the proposed changes never accidentally drift into them.

- **Flow / navigation.** Sidebar (All projects + project list + ＋New project + status pill) → main pane (dashboard or project view) → modals (new project, transcript viewer). It's coherent and matches the data model.
- **Color palette.** Dark surfaces, purple accent, semantic colors for success/warn/danger. Tokens are clean and there's no contrast issue to fix.
- **Project model.** `Project` = name + folders[] + WhisperConfig + auto_run + AC + volumes + exclude_patterns + ordering + YouTube settings. Persisted to `projects.json` + per-project `state.json`. This shape has held up through every feature added.
- **Deterministic queue.** One worker, serial, condition-gated. Pause / resume / prioritize / skip all work. This is the spine and it's correct.
- **Three-tab transcript modal.** Transcript / Analysis / Narrative is the right surface for "view a result." The tabs themselves stay; their discoverability is the issue (see P5).
- **Hallucination warning banner.** Auto-detected, surfaced inline, links to the "re-transcribe with --no-context" CTA. Don't touch.

---

## 3. Pain points

Each pain point is named with the user-flow that surfaces it. Numbered
so proposed changes (§4) can reference them.

**PP1 — "What just finished while I was away?"**
Studio runs 24/7 via launchd. Dashboard's "Now playing" only shows the
in-flight file; there is no recent-completions view. State already has
`completed: {path: {completed_at, duration_sec}}` per project — the
data is there, the UI doesn't surface it.

**PP2 — "Where does Information Studio's fallback work show up?"**
Info Studio's `RESEARCH.md` (sibling repo) says it will POST URLs to
`/api/projects/<pid>/youtube` when YouTube native captions are missing.
Studio has zero UI distinction between human-submitted and external-
submitted jobs. The operator can't see "what is Info Studio doing in
my queue right now," can't tell which project to point Info Studio at,
can't copy the endpoint without reading source code.

**PP3 — "How do I find that session where I talked about X?"**
The files-card search box at `app.js:309` filters filenames only.
Content search across transcripts does not exist. For a corpus
accreting toward 4 years of recordings, this is a real daily friction.

**PP4 — "Which file is queued next?"**
File list shows status per row but no queue position. State has
`priority_queue: [paths]` and a deterministic ordering, but neither is
visible. ↑ moves a file to the front; the file doesn't visibly move.

**PP5 — "How do I re-transcribe a whole project with new settings?"**
Per-file re-transcribe exists; bulk re-transcribe does not. Bulk
actions are in HANDOFF.md's pending list as task #3 and still unbuilt.

**PP6 — "What's the difference between Local Files / YouTube / Music?"**
Three sibling tabs imply three sources. But Music is a *mode* of
YouTube ingest, and music-mode files (`<project>/youtube/<title>/vocals.mp3`)
are scanned by the local scanner anyway. The tab structure encodes a
mental model that doesn't match reality.

**PP7 — "Why is this project paused?"**
Worker pauses for AC power, missing volume, or manual pause. The badge
says "Paused" without the reason. The user has to remember their own
project's conditions to debug it.

**PP8 — "How do I configure the Anthropic key / Ollama URL?"**
No global settings drawer. Ollama URL is hardcoded; ANTHROPIC_API_KEY
is read from env only. Setting up the narrative layer requires reading
the README. No surface to test the connection or see whether the key
is loaded.

**PP9 — "What's available for me to analyze / narrate?"**
Analysis + Narrative are buried behind opening a transcript. There's
no list of "transcripts that have analysis", no batch trigger, no
project-level "auto-analyze on completion" toggle. Features that were
expensive to build see less use than they should.

**PP10 — "Did Studio successfully ingest the URL I just pasted?"**
URL panel is fine in flight. But completed URL rows pile up underneath
indefinitely — `SPEC_YOUTUBE_AND_MUSIC.md` calls out "clear completed"
as a needed button; it's not built. Long-running projects will eventually
scroll a 100-row history.

---

## 4. Proposed changes (ranked impact ÷ effort)

Six proposals. Each names the pain points it addresses. Effort
estimates assume a single focused worktree session per proposal; they're
relative, not calendar-time.

### P1 — Inbox replaces the dashboard hero
**Addresses:** PP1, PP2 (in part), PP10
**Impact:** high — answers the two questions you have when you open the app
**Effort:** medium

The dashboard's hero today is "global metrics tiles + project cards" —
sparse for 1-project users, uninformative for the "I walked away for an
hour" case. Replace the hero with a unified **Inbox** view (the global
metrics tiles and project-card grid move below, not away):

```
┌─ Now transcribing ─────────────────────────────────────────┐
│ samvaad / session_2025-09-12.m4v · chunk 3 of 7 · 04:21    │
│ [progress bar]                                              │
└─────────────────────────────────────────────────────────────┘

┌─ Up next (3) ──────────────────────────────────────────────┐
│ #1  samvaad / session_2025-09-15.m4v   ← priority, you      │
│ #2  music   / vocals.mp3                ← YouTube job        │
│ #3  podcasts / Ep_47.mp3                ← scanner order      │
└─────────────────────────────────────────────────────────────┘

┌─ Recently finished (24h) ──────────────────────────────────┐
│ ✓ samvaad / session_2025-09-10.m4v   12 min ago   53 min   │
│ ✓ podcasts / Ep_46.mp3               1 h ago      28 min   │
│ ⚠ music   / track_05.mp3             3 h ago      hallucination │
│ … 7 more ▾                                                  │
└─────────────────────────────────────────────────────────────┘

┌─ External submissions ─────────────────────────────────────┐
│ ⏵ Information Studio · 2 URLs in flight                    │
│ ⏵ Information Studio · 5 URLs done in last 24h             │
└─────────────────────────────────────────────────────────────┘

(below: existing metrics tiles + project-card grid)
```

All the data is already in state (`completed`, `priority_queue`,
`current`, `youtube_urls`). This is a new view over existing JSON
plus one route to enumerate "up next" across projects (the scanner
already orders within a project; "what's globally next" is just the
worker's actual next pick).

The External submissions strip only renders if there's been any
`submitted_by != "user"` activity — keeps the dashboard clean for
solo users.

### P2 — Fallback-transcriber affordances for Information Studio
**Addresses:** PP2, PP8 (in part)
**Impact:** high (v2's defining new use case)
**Effort:** medium

Information Studio will POST `{url, mode}` to
`/api/projects/<pid>/youtube`. Studio's UI currently doesn't expose
the multi-tenant nature of its queue at all. Four bundled affordances:

1. **`submitted_by` on URL rows.** Schema addition to the YouTube URL
   row: `submitted_by: "you" | "<external client name>"`. Backend
   accepts an optional `X-Studio-Client` header on the POST and records
   it. UI renders a small chip on the row ("you", "Information Studio",
   "MCP: claude-code", etc.).
2. **"Fallback transcriber" project preset.** New-project modal gets a
   preset chooser at the top: **Personal corpus** (default — folders +
   optional YouTube) | **Fallback transcriber** (speech-only YouTube
   ingest, no human folders, marked intent). Picking the preset
   pre-fills sensible defaults; the user still names the project and
   confirms.
3. **Settings drawer.** New global modal anchored to a gear icon in the
   sidebar footer (next to the version pill). Exposes:
   - Studio's localhost URL (copy button)
   - Per-project `pid` table with one-click copy of the full
     `POST http://127.0.0.1:5180/api/projects/<pid>/youtube` line
   - `ANTHROPIC_API_KEY` status (loaded / missing) — read-only
   - Ollama URL + test-connection button
   - Version pill + log file path
4. **External-submission strip** in the Inbox (see P1).

None of this changes the project model. The `submitted_by` field is
purely additive. The preset is a UI affordance over the existing
schema. The settings drawer just surfaces things that already exist
(or are read from env / config).

### P3 — Always-visible actions + visible priority position
**Addresses:** PP4, and the discoverability problem under §1.4
**Impact:** medium-high
**Effort:** low

Two paired changes to the files table:

- **Always-visible icon column** (right side of every row, fixed
  width). Three small icon buttons: ↑ (prioritize), ↻ (re-transcribe),
  ⌕ (view). Replace the current `.actions` hover-reveal pattern at
  `app.js:651–657, 699–701`. Icons get hover labels for new users.
- **Priority position chip.** For any file in `state.priority_queue`,
  render a `#1`, `#2`, `#3` chip on the row. Files not in the priority
  list show no chip. Click on the chip opens a tiny popover: "Move up
  / Move down / Remove from priority".

This makes the priority feature visible for the first time, kills the
"hidden controls" problem, and costs maybe 30 lines of CSS + a small
data join in `renderFilesTable()`.

### P4 — Keyword search across transcripts (filename + body)
**Addresses:** PP3, and partly PP9 (find a transcript to analyze)
**Impact:** medium — opens up the corpus
**Effort:** low-medium

Two surfaces:

- **Project-scoped search.** Existing files-card search box at
  `app.js:309` gains a small toggle: **Match:** `filename` | `content`
  | `both`. In `content` / `both` mode, results show a single-line
  snippet of the matching context under the filename. Implementation
  is one Flask route that walks the project's folders'
  `transcriptions/*.txt` and yields `(file, line_number, snippet)`
  tuples. No LLM, no index, no ranking by meaning — straight grep.
- **Global search.** A search icon in the sidebar footer (or `Cmd-K`
  shortcut) opens an overlay that searches all projects. Results
  grouped by project, each clickable into the transcript modal scrolled
  to the match.

Explicitly out of scope: semantic / meaning-based search. That's
TherapyTalks territory and stays there. Studio's job is "find this
string in my transcripts" — no more, no less.

Hallucination-flagged transcripts get a filter checkbox to exclude
them from search results (they're often dominated by repeated loops
and otherwise pollute results).

### P5 — Promote YouTube + collapse the Music tab
**Addresses:** PP6, and indirectly the YouTube-is-first-class case
**Impact:** medium
**Effort:** low

YouTube ingest is buried in `<details>Advanced</details>` at
`index.html:88`. Music mode is a sibling tab next to Local Files and
YouTube, implying three sources when there's really one source in two
modes. Three changes:

- **Move "Enable YouTube ingest"** + default mode out of the Advanced
  details and into the main section of the New Project modal,
  alongside Model and Language. Reword as "YouTube source: off /
  speech / music".
- **Drop the Music tab.** Music-mode files already live in
  `<project>/youtube/<title>/vocals.mp3` and get scanned by the local
  scanner. They appear in **Local Files** automatically. Add a small
  `mode` chip ("speech" / "music") on any row whose path includes
  `/youtube/`. The Status filter dropdown gains a "Mode: any / speech
  / music" facet.
- **Music players belong in the Transcript modal.** The current Music
  card layout (`app.js:640–660`) with embedded audio players for
  vocals + instrumental moves into the Transcript modal as an audio
  widget at the top, rendered only when the row is a music-mode file.
  That's the right place for "play this while reading the transcript."
- **Two visually-identical add rows is awkward.** Today the YouTube
  panel exposes the "+ Add another playlist" form on top and the
  single-URL "Add URL" inbox immediately below. Both are 1-line
  forms with [URL input] [Speech/Music select] [Add button] — visual
  duplicates that imply the user has to choose between two near-
  identical controls. Collapse them in v2: one input that auto-
  detects playlist vs. single video from the URL shape (`/playlist?`
  vs. `/watch?`), with a single Add button that routes to the right
  endpoint server-side. Mode select stays. Removes the second row
  entirely and removes the disclosure link too.

Result: one file list, one mode tag, one add row, music UX improves
rather than regresses.

### P6 — State honesty (idle / paused / empty / scan)
**Addresses:** PP7, PP1 (in part), and the empty-state catalogue
**Impact:** medium
**Effort:** low

A bundle of small honesty fixes:

- **Idle "Now playing"** → "Idle since `<time>` · next pick: `<file>`
  (waiting for `<reason>`)". When truly idle: "Caught up — 0 pending
  files across all projects." Currently it just goes blank.
- **Pause-reason chip.** When a project shows "Paused", append the
  reason: "Paused — waiting for AC power" / "Paused — `/Volumes/Drive`
  not mounted" / "Paused — by you". Worker already knows; UI needs to
  ask.
- **No-projects state** at `app.js:223` → a 3-step guided start:
  > **Step 1.** Click ＋ New project.
  > **Step 2.** Point it at a folder of media (or paste a YouTube URL after enabling YouTube ingest).
  > **Step 3.** Studio scans automatically and starts transcribing — leave it open or close the tab; the queue runs in the background via launchd.
- **Last-scan timestamp** in the project header — "Last scanned 4 min
  ago" with a "Rescan now" link. `watcher.py` already does periodic
  scans every 5 min; the UI just never says so.
- **Backend offline banner** at `app.js:61` gets an actionable next
  step: "Check `logs/launchd.err.log` or open Repair.command."
- **"Clear completed YouTube history"** button in the YouTube panel
  (SPEC_YOUTUBE_AND_MUSIC.md item, still unbuilt) — fixes PP10.

---

## 5. Out of scope for v2 (explicit preserve list)

- **Flow / navigation pattern** (sidebar + main + modals stays exactly as today).
- **Color palette / design tokens** (`style.css:6–58` untouched).
- **Project model** (Project + ProjectState + WhisperConfig schemas).
  Additive only: P2's `submitted_by` field on URL rows is the single
  schema addition, and it's backward-compatible.
- **The deterministic queue's semantics.** One worker, serial,
  condition-gated. No new concurrency, no agent-driven file picking.
- **The principle "Studio does one thing: transcription."** Analysis
  and Narrative tabs stay as sidecars on the transcript modal, never
  promoted to top-level workflows.
- **No new frontend framework.** Vanilla JS as today.
- **TherapyTalks-class features** (cross-project pattern detection,
  semantic / meaning-based search, theme arcs, "find sessions where I
  mention X by meaning"). Those belong to TherapyTalks consuming the
  Studio's outputs. Studio's job is clean transcripts and a legible
  queue.
- **MCP server itself.** Decision #11 in `GATE_CHECKIN.md` keeps MCP
  exposure on a separate track. The settings drawer in P2 surfaces the
  HTTP endpoints in a form that's MCP-ready, but actually shipping an
  MCP server is downstream of this review.

---

## 6. Open questions for the user

The four big direction decisions are locked (Inbox-replaces-hero,
Fallback-preset+labels, Always-visible-icons+chips, Keyword-grep
in-scope). Five smaller calls remain — each has a lean, but worth
explicit sign-off before the worktree starts:

1. **Auto-create the fallback project?** When Studio detects Info
   Studio on the machine, should it auto-create an
   `info-studio-fallback` project on first run, or only when the user
   picks the preset in the New Project modal? *Lean: user-triggered —
   preserves the "user owns project creation" principle.*
2. **Cmd-K search default scope.** Project-scoped, or global? *Lean:
   project-scoped from a project view, global from the dashboard, with
   a toggle in the overlay.*
3. **Settings drawer anchor.** Gear icon in the sidebar footer (next
   to the version pill), or a new top-level sidebar item? *Lean: gear
   icon in the footer — keeps the sidebar's project-list focus
   uncluttered.*
4. **External-submission visibility window.** How far back does the
   Inbox's External Submissions strip look? *Lean: in-flight URLs +
   last 24h done, capped at 10 rows. Older rows are reachable via the
   project's YouTube panel.*
5. **Auto-analyze toggle.** Should there be a project-level "auto-run
   Ollama analysis on transcription completion" toggle? *Lean: yes,
   project-level, default off. Pairs naturally with the Analysis tab
   being underused (PP9).*

---

## 7. Potentially abandoned / unfinished — flagged, not assumed

Things that look like work-in-progress or "good first task" entries
that never landed. Flagging here so v2's worktree session can either
absorb them or explicitly defer them.

- **MCP server.** Referenced in HANDOFF.md, AGENT_APPROACH_BRAINSTORM.md, decision #11 of GATE_CHECKIN.md, and in Info Studio's RESEARCH.md as "planned-but-not-built." HTTP routes are deliberately MCP-shaped (`main.py:425–427`) but the MCP server itself does not exist. *Lean: deferred; P2's settings drawer exposes the HTTP endpoints cleanly enough that MCP is purely additive.*
- **Recent activity feed.** HANDOFF.md good-first-task #5. *Absorbed by P1.*
- **Manifest export.** HANDOFF.md task #6, no code. *Defer.*
- **Concurrency knob.** HANDOFF.md task #4, no code. *Defer — concurrency-is-1 is correct for Whisper on one Mac.*
- **Bulk file actions.** HANDOFF.md task #3, no code. *Defer to a v2.1; PP5 is real but lower priority than P1–P6.*
- **`.app` icon.** HANDOFF.md task #7, no code. *Defer — cosmetic.*
- **Settings UI (edit-after-create).** HANDOFF.md task #1, current state is the per-project create-time modal only. *Partially addressed by P2's settings drawer for global concerns; per-project edit-after-create still pending and deferred.*
- **Folder picker.** Exists as a small "Browse…" button (`index.html:54`). *Works. Underplayed — would benefit from being slightly more prominent but not blocking.*
- **Backfill bug.** Known issue in HANDOFF.md: completed transcripts that pre-exist on disk aren't reflected in `state.json` after first run. *Bug, not a feature gap — fix during P1's "Recently finished" data plumbing, since that surface will expose the bug visibly.*
- **"Clear completed YouTube history"** button. Called out in `SPEC_YOUTUBE_AND_MUSIC.md`, not built. *Absorbed by P6.*
- **Disk usage column.** Flagged as v2 in spec, not built. *Defer.*

---

## Critical files referenced

Frontend:
- `app/static/app.js` — SPA controller (1397 lines)
- `app/static/style.css` — design tokens + layout (1081 lines)
- `app/templates/index.html` — shell + 2 modals (180 lines)

Backend:
- `app/main.py` — Flask routes (837 lines)
- `app/projects.py` — Project / ProjectState / WhisperConfig dataclasses
- `app/yt_ingest.py` — YouTube ingest module (Info Studio's fallback target)
- `app/scanner.py` — file discovery (status annotation, source of the backfill bug)
- `app/narrate.py` — Claude Opus narrative layer
- `app/watcher.py` — periodic folder scanner (5 min cadence)

Context:
- `HANDOFF.md` — pending tasks list (treated as canonical)
- `SPEC_YOUTUBE_AND_MUSIC.md` — YouTube spec, currently at Gate 3
- `GATE_CHECKIN.md` — decision log #1–#11
- `AGENT_APPROACH_BRAINSTORM.md` — agent layer thinking (consumer of Studio)
- `/Users/ruchir/Documents/cowork-tools/information-studio/RESEARCH.md` — Info Studio's view of Studio's fallback HTTP API

---

## Quality bar this doc holds itself to

- Every screen in §1 has a file:line ref a reader can click.
- Every pain point in §3 cites an observed flow.
- Every proposed change in §4 names the pain points it addresses.
- §5 (out of scope) explicitly names every "locked good" item.
- Open questions in §6 each carry a lean; the user can ratify or
  redirect without re-deriving.
- Unfinished features in §7 are flagged, not silently assumed.

When v2's worktree session opens, this doc is the entry point. Each P*
becomes one (or a small cluster of) commits, each independently
reviewable per the project's worktree-isolated workflow.
