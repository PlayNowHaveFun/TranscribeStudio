# Gate check-in — YouTube ingest & music mode

Companion to `SPEC_YOUTUBE_AND_MUSIC.md`. Defines the staged gates this
feature has to pass before it's considered shipped, the exit criteria
at each stage, and a running decisions/risks log so future sessions
(yours or an agent's) can pick up without re-deriving context.

Owner: Ruchir
Opened: 2026-05-13
Status: **Gate 0 — Spec review** (current)

---

## Stage gates

Each gate has an explicit exit criterion. Don't advance until it's
green. The whole point of writing this down is to resist the urge to
slide forward when something feels almost-done.

### Gate 0 — Spec review *(current)*

Goal: confirm the design is right before any code changes land.

Exit criteria:

- [ ] `SPEC_YOUTUBE_AND_MUSIC.md` read end-to-end and edited where
      it drifts from intent.
- [ ] Four decisions in this doc's "Decisions log" all marked
      **decided** (none `tentative`).
- [ ] Open questions list in the spec is short enough to live with —
      each remaining item has a fallback default written down.
- [ ] One human (Ruchir) explicitly signs off below.

Sign-off:
```
[ ] Ruchir — _date_
```

### Gate 1 — Git baseline

Goal: the project is under version control with a clean snapshot of
the pre-feature state, so the new work can be reverted as one unit if
needed.

Exit criteria:

- [ ] `git init` run in the project root (currently not a repo).
- [ ] `.gitignore` written, covering at minimum: `venv/`, `data/`,
      `logs/`, `__pycache__/`, `.DS_Store`, `*.pyc`.
- [ ] Existing files committed as the first commit (`baseline:
      pre-youtube-feature`).
- [ ] Tag `pre-youtube-feature` placed on that commit.
- [ ] Feature branch `feature/youtube-ingest` created off baseline.
- [ ] All work after this point lands on the feature branch; merge to
      `main` is a separate gate.

Notes: this is a project that's been worked on without git up to now,
so the initial commit is large. Worth a one-time review of what's in
the working tree before committing.

### Gate 2 — Prototype on the bench

Goal: end-to-end happy path works for one URL, in isolation, before
touching the worker.

Exit criteria:

- [ ] `app/yt_ingest.py` module exercised via a one-off test script
      (or pytest): speech mode, one URL, produces `source.mp3` +
      `metadata.json` in the right folder.
- [ ] Same module, music mode flag: produces `vocals.mp3` +
      `instrumental.mp3` cleanly. Demucs binary check passes.
- [ ] Audio files appear in a Samvaad-equivalent project folder and
      get picked up by the studio's scanner on its next tick.
- [ ] Whisper engine transcribes the URL-sourced file with no code
      changes — proves the "drop into existing pipeline" assumption.
- [ ] One end-to-end run from each of the five test cases in the
      spec is run manually.

What this gate *doesn't* prove: UI, state management, error paths,
concurrency. Those are the next gate.

### Gate 3 — Studio integration

Goal: the feature is wired into the project model, worker, state, and
UI.

Exit criteria:

- [ ] `Project` and `ProjectState` schema additions in place,
      backward-compatible with existing `projects.json` /
      `state.json` (verify by loading the existing Samvaad project).
- [ ] Worker handles the three job kinds (download, separate,
      transcribe) and picks them via the extended `next_pending`.
- [ ] New API routes return correct payloads (manual curl).
- [ ] Project settings UI exposes the new toggles.
- [ ] "Add URL" textbox in the project view submits and shows status.
- [ ] Live status panel renders `download_progress` /
      `separate_progress` events.
- [ ] Cancellation works mid-download and mid-separation.
- [ ] All three new studio-integration test cases from the spec pass.

### Gate 4 — Impact regression

Goal: existing features still behave identically when YouTube ingest
is **disabled** on every project.

Exit criteria:

- [ ] Samvaad project, freshly loaded, behaves identically to
      pre-feature: same files queue in the same order, same configs.
- [ ] Existing `transcribe.sh` / `bin/transcribe-engine.sh` reference
      still runs against the same folders (not on the live path, but
      not broken).
- [ ] Worker latency on a normal file is unchanged (no spurious
      polling, no new locks held).
- [ ] Hallucination detector still flags the same set of known-bad
      transcripts in the existing corpus.
- [ ] `Install.command` / `Repair.command` / `Uninstall.command` all
      complete with no new error messages.

### Gate 5 — Ship

Goal: merge to main and run for one week without intervention.

Exit criteria:

- [ ] Code merged from `feature/youtube-ingest` to `main`. Feature
      branch tagged and not deleted (cheap insurance).
- [ ] README.md updated with the new feature, install steps for
      yt-dlp/demucs, and one screenshot.
- [ ] At least 10 URLs ingested across both modes without manual
      intervention.
- [ ] No regressions reported in the Samvaad backfill during the
      same week.

---

## Decisions log

Decisions already made and worth not relitigating. Each entry: what we
chose, why, and what we explicitly *didn't* choose.

| # | Decision | Choice | Rationale | Rejected alternatives |
|---|----------|--------|-----------|----------------------|
| 1 | URL→project mapping | Per-project URL inbox | Reuses projects, conditions, scanner; folds into the existing UI without a new top-level surface | New source-type abstraction on Project (too invasive for v1); global URL queue (loses project context) |
| 2 | Download location | Inside the target project's first folder, under a `youtube/` subdir | Project-centric: one place to find all of a project's media; scanner picks it up for free | Studio-owned root under `data/` (separates transcripts from media); original spec's `~/Documents/yt-tool/` (decouples from projects) |
| 3 | Worker concurrency | Same serial worker, new stage types | Simple, predictable, inherits all pause/condition behavior; matches existing model | Separate ingest thread (more throughput, more state to manage); download-on-submit (slow UI, blocks HTTP) |
| 4 | Gate check-in form | Process doc (this file) + git baseline tag | Process doc captures intent + checklist; git tag is the technical revert button | Feature-flag-only (operational, not a planning artifact); all three (overkill for solo work) |
| 5 | Music-mode default for `--no-context` | Forced on, controllable per-project | Lyrics are whisper's worst case for hallucination loops; `no_context` is the proven fix | Leave to user (likely-bad defaults out of the box); silently apply (hides the trade-off) |
| 6 | Music-mode transcription target | `vocals.mp3`, not `source.mp3` | The whole point of separation — gives a clean lyric track that pairs with the instrumental as a karaoke `.srt` | Transcribe `source.mp3` (re-introduces the problem Demucs solved) |
| 7 | Collision policy | Auto-append `_<video_id_hash[:6]>`, no prompt | UI app shouldn't fail interactively for an avoidable problem | Original spec's exit-with-error (correct for CLI, wrong here) |
| 8 | Dependencies install path | Manual `brew install yt-dlp` / `pip install demucs`; lazy CTA in UI; never install from inside launchd Python | Auto-install inside the sandbox has failed reliably enough that the audio-transcribe skill documents it | Bundle a `requirements.txt`-style auto-install (will fail under launchd) |
| 9 | Ingest entrypoint | Python module only (`app/yt_ingest.py`), imported by the worker. No standalone CLI. | Studio is the only intended entry surface; skipping the CLI avoids argparse plumbing, import-side-effect hygiene, and Flask-vs-CLI dependency split | Shell-invokable `bin/yt-ingest.py` (extra surface area for no v1 use case) |
| 10 | Same URL submitted to two projects | Re-download into each project's `youtube/` folder; each project owns its own copy | Simplicity beats the small disk savings; cross-project symlinks make lifecycle/state confusing (delete one project, the other points at thin air) | Symlink the second project to the first project's audio (fragile); refuse the second submission with an error (user-hostile) |
| 11 | MCP exposure of YouTube routes | In scope for the MCP server track. When studio routes get wrapped as MCP per `AGENT_APPROACH_BRAINSTORM.md`, `add_youtube_url(project_id, url, mode)` is part of that surface. v1 of this feature only ensures the HTTP route is shaped so MCP can wrap it trivially. | Closes the gap between the studio and the agent layer; lets Claude submit URLs on your behalf when the MCP server lands | Defer indefinitely (leaves a known gap that would need re-thinking later); build the MCP server now inside this feature (scope creep) |

---

## Risks

Things that could surprise us, sorted by how much they'd hurt.

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| Demucs blocks the queue for too long, slowing therapy-session backfill | Medium — Samvaad backfill could lag | Medium | Spec'd as known trade-off. Mitigation: live status shows what's blocking. Escape hatch: v2 splits the worker. |
| yt-dlp breaks on a YouTube format change | Medium — feature dead until fixed | Low (yt-dlp updates fast) | Document the `brew upgrade yt-dlp` step; surface yt-dlp version in settings UI. |
| Demucs install fails on first launch (PyTorch+~2GB) | High — feature unreachable | Medium | "Run setup" CTA streams output, doesn't hide errors. Speech mode still works without demucs. |
| Music-mode transcripts trigger the hallucination detector falsely | Low — UI banner is informative, not a hard block | High | Spec says: keep the banner, it's actually correct that vocal-stem outputs are noisier. |
| User pastes URL into a project whose `translate_to_english=true` and gets English lyrics they didn't want | Low — visible immediately in the transcript | Medium | Settings UI warning when `mode=music + translate=true`. |
| Disk fills up from accumulated downloads | Low — easy to delete | Low at low URL volume, high at high | Add disk-usage column to project view as a v2; for v1, just note it. |
| `youtube_urls` history makes `state.json` slow to parse | Very low — JSON is small | Low | "Clear completed" button; numerically not a real concern below ~10k URLs. |
| Concurrent project edits race with the worker reading state | Low — already handled by the existing `_lock` | Low | Same locking pattern as today. |

---

## Open questions for the next gate

All three of the original open questions were resolved during Gate 0
review and moved into the Decisions log above (entries 9, 10, 11):
- CLI parity → no standalone CLI, Python module only
- Cross-project de-dupe → re-download
- MCP exposure → in scope for the MCP server track

Remaining open questions are now spec-internal only (see
`SPEC_YOUTUBE_AND_MUSIC.md`): history retention default; karaoke-video
v2 timing.

---

## Sign-off ladder

When each gate closes, initial here. The point isn't ceremony — it's
that you have to stop and look at the checklist before moving on.

```
Gate 0  Spec review              [ ] Ruchir   ___________ (date)
Gate 1  Git baseline             [ ] Ruchir   ___________
Gate 2  Prototype on the bench   [ ] Ruchir   ___________
Gate 3  Studio integration       [ ] Ruchir   ___________
Gate 4  Impact regression        [ ] Ruchir   ___________
Gate 5  Ship                     [ ] Ruchir   ___________
```
