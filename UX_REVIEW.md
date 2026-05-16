# Transcribe Studio — UX Review

**Audit date:** 2026-05-16
**Scope:** Refinement audit for the next release. Not a redesign.
**Method:** Direct read of `app/templates/index.html`, `app/static/app.js` (1397 lines), `app/static/style.css` (1081 lines), `app/main.py`, `app/analyzer.py`, `app/narrate.py`, `app/watcher.py`, `app/transcriber.py`, plus `git diff main..feature/queue-panel` and `git diff main..feature/loading-spinners` for in-flight PR work. Every claim below carries a file:line citation.

---

## 1. What the prototype gets RIGHT

Don't regress on any of these in the next release.

- **Cohesive dark "Glimmering Sunrise" token system.** Variables for surface, text, accent, and semantic colors are clean and consistent (`app/static/style.css:6-57`). Purple `#7c3aed` reads as a signature, not arbitrary. Typography is dialed in (`style.css:69-74`) with no font-size drift across the codebase.
- **Diff-aware DOM rendering on the 2-second status poll.** The sidebar nav (`app.js:99-122`), dashboard project cards (`app.js:227-247`), and "Now transcribing" card (`app.js:490-528`) all rebuild structure only when the underlying set changes, and otherwise patch values in place. This is why the UI never flickers under continuous polling.
- **"Now transcribing" card with append-only log tail that preserves scroll.** The log buffer updates without resetting the user's scroll position unless they were already at the bottom (`app.js:522-528`). This is the kind of detail that distinguishes a tool from a toy.
- **Narrative caching with shared scaffold across styles.** The scaffold JSON is computed once per transcript and reused across all four narrative styles; only the prose `.narrative.<style>.md` is per-style (`app/narrate.py:269-278`, `app.js:1087-1088`). Switching styles is instant for cached results, and regenerating one style doesn't waste a scaffold pass.
- **Quality-warning banner for hallucinated transcripts.** When transcript detection flags consecutive repeats or low diversity, the modal renders an inline warning with the suggested fix (`app.js:1019-1028`). This is real product-thinking baked in.
- **Source-type tabs (Local / YouTube / Music) with inline audio players on Music cards.** Tab switching is local-state, no roundtrip (`app.js:545-567`). Completed music files render both vocals and instrumental audio elements inline (`app.js:630-638`).
- **Native macOS Finder folder picker on Add Project.** `/api/dialog/pick-folder` replaces the path-typing experience (`app.js:1316-1328`, commit `ee94f19`). The fallback for browsers that block `file://` URLs is `open -R` shelled out from `/api/reveal` (`app/main.py:629-657`).
- **Sensible re-transcribe with confirm dialog.** Users can't accidentally delete a transcript (`app.js:717-721`, `672-676`).
- **Status-aware sidebar pill** (`app.js:72-77, 83-96`) — running / paused / idle / on battery / error, with a meaningful "on battery" state when `require_ac_power` is set.

---

## 2. Current friction points

Severity legend: **blocker** = stops a real workflow / **annoyance** = noticeable rough edge / **nit** = polish.

| # | Issue | Location | Severity | Why it's friction | Proposed fix |
|---|---|---|---|---|---|
| 1 | Errors universally fire as native `alert()` | 16 sites in `app.js`: lines 893, 901, 911, 920, 939, 946, 964, 1116, 1134, 1141, 1268, 1327, 1354, 1355, 1361 | **annoyance** | Blocks the modal, can't be styled, no inline retry, dismissing loses context. Analyze/Narrative failures (1134, 1141, 1268) are the worst because the user wants to re-run from the same place. | Shared inline-error banner component (`<div class="error-banner">{message}<button>Retry</button></div>`). Use it everywhere alert() lives today. |
| 2 | 5-minute folder watcher is invisible | `app/watcher.py:22, 51-82` | **annoyance** | The watcher silently scans every project every 5 min and only logs to `logs/studio.log:78`. Users don't know it exists, so they manually click Refresh anyway. | Last-scanned timestamp in the sidebar footer (next to `#port-info`, `index.html:31`). Optional toast when a scan discovers new files. |
| 3 | Analyze and Narrative are buried 4 clicks deep | Sidebar → project → file row → modal → tab → button. Each modal tab keeps the bare label "Analysis" / "Narrative" regardless of cached state (`index.html:138-140`) | **blocker** for discoverability | The most differentiated feature in the app is invisible until you happen to open a transcript and click the right tab. There's no surface that says "this project has 8 analyses and 2 narratives cached." | (a) Tab labels become `Analysis ✓` / `Narrative ✓ · story` when sidecar exists. (b) Project view shows a small "AI" column on the file table when `.analysis.json` or `.narrative.*.md` exists. (c) The new Insights view (item 7) is the long-term answer. |
| 4 | No batch actions on files | `app.js:707-731` (re-transcribe, skip, prioritize are per-row buttons) | **annoyance** | Re-transcribing 20 files = 20 confirms. Skipping a batch of failures = 20 clicks. | Add row-level checkboxes + toolbar with "Re-transcribe selected / Skip selected / Prioritize selected". Reuse the multi-select pattern from the new Insights view. |
| 5 | "Show in Finder" only works for media files | `app.js:1050-1066` opens via `/api/reveal` for the audio/video path only | **annoyance** | No way to reveal the project folder, no way to reveal an `.analysis.json` or `.narrative.<style>.md` sidecar. Sidecars are completely invisible on disk from the UI. | Add "Show project in Finder" button to project view header (`app.js:280-286`). Add "Show analysis file" / "Show narrative file" links inside the Analyze/Narrative panels next to their results. |
| 6 | PR #4 (loading spinners) and PR #5 (queue panel) sit in DRAFT | `gh pr list` shows both DRAFT | **blocker** | These already fix obvious problems and ship features the user explicitly asked for (ecdda is done in PR #5). Not merging them is itself the biggest friction. | Promote both to ready-for-review, address any feedback, merge. |
| 7 | Add Project modal is dense | `index.html:42-126` — name, folders (with browse), model, language, 3 checkbox toggles, then 4 collapsibles (Advanced / YouTube / AI Analysis) | **annoyance** | The 80% case is "pick a folder, accept defaults". The form makes that feel like a setup wizard. | Collapse Model/Language behind "Advanced" by default; lead with name + folders + Browse. Remember last-used settings and prefill. |
| 8 | Narrative generation can be lost on modal close | `app.js:1120-1144` — `generateNarrative()` resolves to the open modal; closing the modal mid-generation drops the result UI even though the backend completes | **annoyance** | Users wait 30-90s for Opus, then need to leave the modal, then come back to find no UI state and have to click Generate again (it'll hit cache, but the UX feels broken). | Backend already writes the sidecar before responding (`narrate.py:281`). On modal reopen, `tryLoadCachedNarrative()` already reads it. The fix is to either (a) keep the network request alive in the page-level state, not the modal, or (b) detect cached-on-reopen and surface a "Result ready" badge. |
| 9 | Refresh status string disappears after one cycle | `app.js:411-418, 430-440` set `refresh-status` text inline, never persists | **nit** | "Found 3 new files" disappears the moment the user navigates away. No history. | Persist last-refresh result + timestamp in a status line that survives view changes. |
| 10 | "Now transcribing" card duplicates between dashboard and project view | `app.js:213` (dashboard call) and `app.js:301` (project view call), both render `now-playing-card` with the same data | **nit** | Visual duplication when one project is active. | Keep on dashboard only, or shrink to a one-line status strip in the project header when active. |
| 11 | Project ordering tag is read-only | `app.js:361` displays `p.ordering` as a tag with no click target. Order is set per-project in projects.py:61 but not editable from the UI | **annoyance** | User sees "newest first" but can't switch to "oldest first" or "alpha" without editing the file list custom order (which then forces "custom" mode). | Click the tag → small popover with the three modes. Persist via PATCH to project settings. |
| 12 | Narrative scaffold `<details>` opens by default | `app.js:1187` — the scaffold (themes, characters, beats, quotes) renders `<details open>` | **nit** | The first thing the user sees is a long structured dump above the prose narrative. The prose is the headline. | Render scaffold collapsed by default; user can expand. |
| 13 | No keyboard shortcuts beyond Escape | `app.js:1372-1373` — Escape closes modals. Nothing else. | **annoyance** | Power-tool feel is missing. Common actions (focus search, switch project, refresh) all require mouse. | Cmd-K project switcher, `/` to focus the file search, Cmd-R to refresh current scope, J/K to move between file rows. Document in the sidebar footer or a `?` overlay. |
| 14 | Zero accessibility scaffolding | `grep -n 'aria-\|role=' index.html app.js` returns nothing | **annoyance** | Modal tabs lack `role="tab"`, panels lack `role="tabpanel"`, the loading spinners lack `aria-live`. Screen-reader users can't navigate. | Add ARIA roles to modal tabs (`index.html:137-141`), `aria-live="polite"` to status text and the now-playing card, `aria-label` on icon-only buttons (`btn-icon` class). |
| 15 | Muted text contrast is borderline | `--text-faint: #55556a` on `--surface: #13131a` (`style.css:6-57`) | **nit** | WCAG AA contrast ratio is ~3.5:1 for muted-faint text on the deepest surface. Just over the line at small sizes. | Lift `--text-faint` to `#6a6a82` or similar. Tiny change, big readability win on the file-table date column and `.muted small` everywhere. |

---

## 3. Information density and hierarchy at scale

### Sidebar
- Width 260px, nav items ~28px tall (`style.css:84, 110-158`). At 900px viewport, **~13 projects** fit before scroll.
- **At 10 projects:** comfortable. **At 50 projects:** flat scroll wall. **PR #5 (feature/queue-panel) introduces project groups with collapse/expand** (`app.js:89-94, 241-264` on that branch) — this is the right fix and the reason that PR must merge before this scale matters.
- Add Project button sits below the project list and gets pushed out of view at ~15 projects (it's not pinned). Worth pinning to the sidebar footer or moving it up.

### Dashboard
- Project cards are ~68px each (`app.js:251-268`). **~10-11 visible without scroll** at 900px.
- The Overview metric row (`app.js:172-180`) stays useful at any scale — it summarizes counts across all projects.
- At 50 projects the cards list is a long scroll with no segmentation. PR #5's groups should flow through into the dashboard too (group section headers in the cards list).

### File table
- Has search + status filter (`app.js:308-318`) — good.
- No virtualization. 500 rows of `<tr>` render fine; 5000 will not. This is not an immediate concern but is worth knowing.
- Per-source tabs (Local / YouTube / Music) further segment the list (`app.js:545-567`). Good.

### Transcript modal
- `<pre id="tx-content">` renders the full transcript as one block (`index.html:144`). For a 90-min transcript (~12,000 words) this is fine; the browser handles it. No timestamp navigation though — the SRT file isn't surfaced.
- Quality-warning banner consumes vertical space when present (`app.js:1019-1028`). At small viewport heights this pushes the tab bar below the fold.

### What breaks first
1. **Sidebar at ~15 projects** — Add Project button gets buried, then the list itself becomes a scroll. Groups fix both.
2. **Dashboard cards at ~30 projects** — long unsegmented scroll. Groups in the cards list fix this.
3. **File table at ~5,000 rows per project** — DOM rendering bogs down. Not urgent; add virtualization if/when it becomes real.
4. **Modal tab bar at <800px viewport height** — the quality banner + tab bar + content fight for space. Lower priority.

---

## 4. The Narrative + Analyze tabs experience

**Honest take:** this is the project's biggest differentiator and the hardest feature to discover.

### Where it lives today
- Both tabs sit inside the transcript modal (`index.html:137-170`).
- Path to use it: sidebar project → file row → modal → tab → button → wait 30-90s (`app.js:1120-1144`).
- Tab labels are static ("Analysis", "Narrative") regardless of whether a sidecar exists. No badge, no count, no cached-state indicator.
- There is no surface anywhere in the app — sidebar, dashboard, project view — that tells you which transcripts have been analyzed or narrated. You'd have to open each one to find out.
- Per memory `project_narrative_layer.md`: this layer is "the first concrete intelligence piece" built on top of the deterministic queue. Per memory `feedback_llm_choices.md`: Claude Opus 4.7 was the explicit, considered choice for narrative quality. The investment isn't matched by the surface area.

### Is this the right surface?
For per-transcript analysis: yes, the modal is correct. You opened a transcript; you want to analyze that one transcript.

For multi-transcript synthesis: no. The modal is per-file and can't see across projects. Backlog cards 68780 ("build narrative from project to project to find organic voice") and a0f30 ("bulk select, parallel analyze, current view becomes one tile") both demand a cross-project surface that doesn't exist today.

### Recommendations
1. **Tab badges for cached state** — `Analysis ✓` / `Narrative ✓ · story` when a sidecar exists (`narrate.py:76-83` writes `.scaffold.json` and `.<style>.md` per-style; `analyzer.py:169-177` writes `.analysis.json`). Cheap, high-signal.
2. **File-table AI column** — small icons or dots in the file row when sidecars exist. Lets users see at a glance which transcripts have been processed.
3. **Dashboard "Recently analyzed / narrated" strip** — a card on the dashboard showing the last 5 sidecars across all projects, with one-click into the modal pre-opened on the right tab. Low effort, big discoverability boost.
4. **New top-level Insights view (Phase 1)** — see item 7 in the prioritized list. This is where multi-transcript synthesis lives.

---

## 5. Cross-cutting themes

### Loading states
- **Good:** Modal Analyze + Narrative panels show a full-panel spinner + label (`app.js:1071-1081, 1215-1226`, PR #4). The "Now transcribing" card has live phase + chunk progress (`app.js:445-528`). YouTube ingest shows phase-aware labels (`app.js:474-488`).
- **Missing:** Per-file in-progress visual in the file table. A file being transcribed shows the `in_progress` tag but no shimmer/spinner. Users glance at the table and can't tell which row is "next" or "actively running" without watching the now-playing card.
- **Missing:** Startup scan loading state (`app/main.py:729-745` runs before Flask is listening, so the browser doesn't even know it happened). A one-shot toast on first connect — "Scanned 12 projects, found 3 new files" — would close the loop.

### Empty states
- **Good:** Dashboard with zero projects (`app.js:223`: "No projects yet…"). File list with zero matches (`app.js:595`: "No files match."). Now-transcribing idle (`app.js:455-458`: "Idle — {reason}").
- **Bare:** Analysis tab with no analysis (`app.js:1220-1226`: "No analysis yet."). Could explain what analysis does and what it costs. Narrative tab is slightly better (`index.html:165-168`) — it mentions the API key requirement.
- **Missing:** Project view with zero files. The file list renders "No files match." but the whole project view stays as a Progress card with all zeros. Could prompt the user to drop files in the folder or refresh.

### Error states
- **Universally bad.** Every error path is a native `alert()` (`app.js:893, 901, 911, 920, 939, 946, 964, 1116, 1134, 1141, 1268, 1327, 1354, 1355, 1361`). No retry, no context preservation, no styling. The Ollama timeout that was in the kanban Done column (card `06d99`) hits the user as `alert("Analysis failed: ...")` with no inline path back.
- **Backend offline is handled okay** — sidebar pill goes red and says "backend offline" (`app.js:61`). But the rest of the UI keeps showing stale data.
- **Re-fix in item #4 of the priority list.**

### Keyboard shortcuts
- Only `Escape` closes modals (`app.js:1372-1373`). Nothing else.
- This is the gap that most signals "prototype" vs "tool I use daily."

### Accessibility
- Zero ARIA roles, zero `aria-label`s, zero `aria-live` regions. The icon-only buttons (`btn-icon` class — re-transcribe, skip, prioritize, ↑) are unlabeled to screen readers.
- Modal tabs use `<button class="modal-tab">` without `role="tab"` / `aria-selected` / `role="tabpanel"`.
- Contrast: `--text` (`#e8e8f0`) on `--bg` (`#0d0d10`) is excellent. `--text-muted` (`#8888a8`) on `--surface` (`#13131a`) is okay. `--text-faint` (`#55556a`) on `--surface` is borderline (~3.5:1).

---

## 6. Prioritized improvement list for the next release

Short-list of 8. Estimates: S = under a day, M = 2-4 days, L = a week or more.

### 1. Merge PR #5 (queue panel + project reorder + groups + custom file order) — **S**
- **Dependency:** none. The PR is in DRAFT and already implements ecdda end-to-end (`feature/queue-panel`, commit `703ce52`).
- **Why:** It's the highest-leverage merge in the queue. Unblocks scale (groups), ships ecdda, introduces drag-drop patterns the rest of the items reuse.
- **Code locations:** sortable wiring in `app.js:159-221, 241-264, 295-318` on the branch; CSS at `style.css:163-198, 206-304`.

### 2. Merge PR #4 (loading spinners on Analyze + Narrative tabs) — **S**
- **Dependency:** none.
- **Why:** Already done, removes the "user left guessing during 30-90s of Opus" failure mode. Fast win.
- **Code locations:** `app.js:1071-1081, 1215-1226` on `feature/loading-spinners`; CSS at `style.css:985-1031`.

### 3. Surface the 5-minute folder watcher — **S**
- **Dependency:** none.
- **Why:** Users don't know the watcher exists, so they manually refresh. Adding a "Last scanned: 2m ago" line in the sidebar footer (next to `#port-info` at `index.html:31`) closes the loop. Optional toast when a scan discovers new files.
- **Code locations:** read `watcher.py:22, 78` for the data; render in `app.js:80-96` (sidebar render).

### 4. Replace all 16 `alert()` sites with a shared inline-error component — **M**
- **Dependency:** none.
- **Why:** Per user decision, this is cross-cutting. One reusable banner used everywhere kills the modal-blocking alert pattern across Analyze, Narrative, queue, YouTube, and project creation.
- **Code locations:** all 16 sites listed in section 2 row 1. New component lives in `app.js` near the modal helpers; styles join `style.css` near the existing `.tx-loading` block.

### 5. Cached-state badges on modal tabs and file rows — **S**
- **Dependency:** none.
- **Why:** Highest-impact discoverability win for the narrative layer. Surfaces invisible work that's already done.
- **Code locations:** modal tabs at `index.html:138-140`, file table rows at `app.js:680-705`. Sidecar existence is on the file metadata payload from `/api/projects/{pid}/files` — `app/main.py` route can include `has_analysis` / `has_narrative` flags.

### 6. Show in Finder for project folder + sidecars — **S**
- **Dependency:** none.
- **Why:** Closes the loop on sidecar files that are invisible to users today. Builds confidence that work is real and durable.
- **Code locations:** project view header `app.js:280-286`; modal Analyze/Narrative panels at `app.js:1219-1252, 1147-1194`. Reuse `/api/reveal` (`main.py:629-657`).

### 7. Cross-project Insights view, Phase 1 — **L**
- **Dependency:** item 1 (groups patterns + multi-select wiring).
- **Why:** Fuses backlog cards 68780 (narrative chain across projects to surface organic voice) and a0f30 (bulk-select, parallel processing, current view shrinks). Ship as a NEW top-level sidebar item ("Insights") first; fold into a redesigned dashboard in a later release.
- **What it does (Phase 1 scope):**
  - Sidebar gets a third top-level entry between "All projects" and the project list.
  - Insights view lists all transcripts across all projects with checkboxes, filterable by project/status/has-analysis/has-narrative.
  - Toolbar action: "Analyze selected" (runs analyzer over selection, sequentially per the user's concurrency decision) and "Generate narrative chain" (runs Opus over selection in order, passing each prior scaffold forward in the prompt so each successive narrative builds context — this is 68780's "find organic voice" mechanic).
  - Output: a synthesis page showing the chain in order, with the per-transcript narratives stacked and a final cross-cutting summary.
- **Deferred to Phase 2:** dashboard redesign where the current view becomes a tile. Parallel LLM execution (a0f30's parallel-thread hint) — see "out of scope" below.
- **Code locations to extend:** `narrate.py` for chain support; new route(s) in `main.py`; new view in `app.js` next to `renderDashboardStructure()` / `renderProjectStructure()`.

### 8. Batch file actions — **M**
- **Dependency:** item 1 (reuses the multi-select component from Insights).
- **Why:** Maintenance scales linearly today. With 500+ files in a project, batch re-transcribe / skip / prioritize is essential.
- **Code locations:** file row checkboxes in `app.js:680-705`; toolbar near `app.js:305-323`; existing per-file API routes already accept arrays (`app.js:715, 720, 724` send `{paths: [path]}`) so the backend doesn't need to change.

### Out of scope for this release
- **Transcription worker concurrency.** Stays at `concurrency=1` per user decision (`app/transcriber.py:24-148`). No parallelism slider, no recommendation to change.
- **Parallel LLM execution for Analyze/Narrative.** a0f30 mentions "analysis also in a parallel thread"; deferred per user direction. The chain in item 7 is sequential by design (each narrative consumes the previous scaffold).
- **From-scratch redesign.** Explicitly excluded — this is refinement.

---

## 7. Backlog mapping

| Backlog card | Status | Maps to release item |
|---|---|---|
| **ecdda** — Drag-drop queue panel | **Effectively shipped** on `feature/queue-panel` (PR #5, commit `703ce52`). The branch implements the queue panel, project reorder, groups, and custom file order in one bundle. | **Item #1: merge PR #5.** Move the kanban card to Done after merge. |
| **68780** — Narrative chain across projects to surface organic voice | Not started. Run-on prompt describing a *content* mechanic: each project's narrative builds on the prior project's voice. | **Item #7: Insights view Phase 1**, specifically the "Generate narrative chain" action. The chain mechanic — passing each prior scaffold forward into the next Opus call — is exactly what 68780 describes. |
| **a0f30** — Bulk select + parallel cross-project analysis; current view becomes one tile | Not started. Run-on prompt describing a *shell* mechanic: new view, bulk-select, parallel execution, current dashboard shrinks to a tile in a bigger picture. | **Item #7: Insights view Phase 1** (bulk-select + cross-project analyze) and a noted **Phase 2** (current view becomes one tile in a redesigned dashboard). The **parallel-thread** sub-piece is explicitly deferred per the user's concurrency decision. |

**Overlap call-out:** 68780 and a0f30 are the same feature in two framings. 68780 is the *content* (narrative chaining to surface voice). a0f30 is the *shell* (cross-project view, bulk-select, parallel). They should land together in item #7, not as separate features. Treating them separately would either duplicate the cross-project view or duplicate the chain mechanic.

**Recommended kanban grooming after this audit:**
- Move ecdda → Done.
- Replace 68780 and a0f30 with a single "Insights view Phase 1" card scoped to item #7 above. Keep the original two prompts as references in the card body — they're useful context for the build.
- Add new cards for items #3, #4, #5, #6, #8.

---

## Appendix: file:line index for quick navigation

| Subject | Primary citation |
|---|---|
| Design tokens | `app/static/style.css:6-57` |
| Sidebar render | `app/static/app.js:80-136` |
| Dashboard structure | `app/static/app.js:158-189` |
| Dashboard live update | `app/static/app.js:191-249` |
| Project view structure | `app/static/app.js:272-338` |
| Project view live update | `app/static/app.js:340-380` |
| Header actions (pause/resume/refresh) | `app/static/app.js:382-441` |
| Now transcribing card | `app/static/app.js:445-530` |
| File list fetch + render | `app/static/app.js:534-731` |
| YouTube panel | `app/static/app.js:733-980+` |
| Transcript modal lifecycle | `app/static/app.js:982-1070`, `app/templates/index.html:128-176` |
| Analyze panel + handler | `app/static/app.js:1219-1271`, `app/analyzer.py:65-179` |
| Narrative panel + handler | `app/static/app.js:1072-1217`, `app/narrate.py:1-360` |
| Show in Finder | `app/static/app.js:1050-1066`, `app/main.py:629-657` |
| Folder watcher | `app/watcher.py:22, 51-82` |
| Startup scan | `app/main.py:729-745` |
| Worker (concurrency=1) | `app/transcriber.py:24-148` |
| Keyboard handlers (Escape only) | `app/static/app.js:1372-1373` |
| All 16 `alert()` sites | `app/static/app.js` lines 893, 901, 911, 920, 939, 946, 964, 1116, 1134, 1141, 1268, 1327, 1354, 1355, 1361 |
| PR #4 diff vs main | `git diff main..feature/loading-spinners` |
| PR #5 diff vs main | `git diff main..feature/queue-panel` |
| Memory: narrative layer | `~/.claude/projects/-Users-ruchir-Documents-cowork-tools-transcribe-studio/memory/project_narrative_layer.md` |
| Memory: LLM choices | `~/.claude/projects/-Users-ruchir-Documents-cowork-tools-transcribe-studio/memory/feedback_llm_choices.md` |
| Backlog cards | `/Users/ruchir/.cline/kanban/workspaces/transcribe-studio/board.json` |
