# Agent approach — brainstorm

A think-piece, not a build doc. Comparing the Flask studio you just got with
what a real *agent* approach would look like, what it unlocks, and how the
two could fit together rather than compete.

## The core distinction

The studio is a **deterministic queue**. Rules in code: "if AC power AND drive
mounted AND not paused, run whisper on the next un-transcribed file." Fast,
predictable, free to run, never surprises you.

An **agent** is a process with a goal, a set of tools, and an LLM in the
loop deciding what to do next. It still uses whisper.cpp under the hood,
but the *orchestration* is an intelligence rather than a script. It can
notice things, judge things, and react.

You can layer an agent on top of the studio. They aren't either/or.

## Three architectural options

### A. Pure local agent loop

A long-running Python process. Each tick: assemble context (state + recent
events), prompt Claude, get back a tool call, execute it, log result, loop.

Tools the agent has:
- `list_pending_files(project)`
- `transcribe(path, params)` — calls the studio's engine
- `read_transcript(path)`
- `summarize(text, style)`
- `tag_session(text)` — therapy / music / interview / voice memo
- `write_index_md(project)`
- `flag_for_review(path, reason)`
- `notify(message)` — macOS notification
- `update_project_config(project, changes)`

The agent's job description (system prompt) becomes the policy. Example:
"Keep all my project transcripts up to date. When a new file finishes,
write a 1-line summary into the project's INDEX.md. If a transcript looks
hallucinated, flag it for re-run with --no-context. If three sessions in
a row mention the same theme, alert me."

### B. Claude Code agent (headless)

You already have Claude Code installed. Run it in headless mode with a
project-level CLAUDE.md that describes what to do, scheduled via `cron`
or an Anthropic Claude Agent SDK harness:

```
0 23 * * *  claude -p "Process new media in ~/Documents/Art/Samvaad. \
    For each new transcript, append a 5-line summary to INDEX.md. \
    If you find a session with strong emotional content, flag it."
```

Claude Code already has Bash + Read + Write + Edit + WebFetch tools. It can
shell out to `whisper-cli`, read transcripts, write summaries, ping you.

Easy to set up — but you're paying for an entire LLM session for what's
mostly deterministic file operations. Maybe $0.20-$1 per nightly run
depending on how much it has to think about.

### C. Hybrid (the recommended mental model)

```
              ┌──────────────────────────────────┐
              │ Agent layer (intelligent)        │
              │ - reads completed transcripts    │
              │ - generates summaries / INDEX    │
              │ - detects themes, flags issues   │
              │ - chats: "what's been on my mind"│
              └────────────┬─────────────────────┘
                           │ MCP / API
              ┌────────────▼─────────────────────┐
              │ Studio (deterministic)           │
              │ - holds the queue, runs whisper  │
              │ - manages projects, conditions   │
              │ - exposes /api/* HTTP endpoints  │
              └──────────────────────────────────┘
```

The studio handles the boring, expensive-to-rethink parts: scan, queue,
run, log. The agent only fires when there's something that genuinely needs
intelligence — a new transcript to read, a quality call to make, a
cross-project pattern to surface.

This keeps cost low (LLM only runs when needed) and behavior predictable
(the queue is still the queue, you can still see exactly what's running).

## What an agent unlocks that the studio can't easily do

1. **Quality judgment.** After a transcript finishes, agent reads it and
   decides: "this is mostly silence — skip; flag the file as 'no useful
   content'." Or "this looks like the loop bug — re-run with --no-context."

2. **Per-file param tuning.** Phone-call quality recordings benefit from
   `medium` not `large-v3`. Quiet voice memos benefit from a smaller chunk
   size. The agent learns your media types over time and adjusts engine
   parameters on a per-file basis.

3. **Semantic indexing.** For each transcript: a one-line summary, key
   topics, named entities, sentiment. Stored in `INDEX.md` per project,
   plus a JSON manifest for AI consumption. Lets you ask "find sessions
   where I talk about my mom" without reading 195 files.

4. **Cross-project pattern detection.** "You've mentioned 'authenticity' in
   four therapy sessions and three music lessons this month. That's the
   theme to bring up next time you want to write."

5. **Conversational interface.** Instead of clicking around the UI:
   - "What did my therapist say about boundaries last week?"
   - "Compare this month's music lessons to last year — am I improving?"
   - "Re-transcribe everything from January with the medium model."
   - "Pull every quote where I said something I'd disagreed with the next day."

6. **Proactive nudges.** "It's been 10 days since your last therapy session
   transcript and there's no new file. Want me to remind you to record?"

7. **Self-improving heuristics.** Agent notices that VAD-off transcripts
   of phone-call recordings tend to fail. Adjusts the project's exclude
   pattern. Documents the change in a journal so you can see what it
   decided and why.

## Cost analysis

For Samvaad's 195 sessions, ~1hr each:

| Operation                       | Tokens (est)   | $ each  | $ total |
|---------------------------------|----------------|---------|---------|
| Per-transcript summary          | 10K in + 200   | $0.003  | $0.60   |
| Per-transcript tag/classify     | 2K in + 100    | $0.0008 | $0.16   |
| Per-transcript theme extraction | 12K in + 500   | $0.005  | $0.97   |
| Cross-project semantic search   | 50K in + 500   | $0.05   | per query |
| Therapy-specific reflection     | 15K in + 1K    | $0.012  | $2.34   |

Backfill the entire corpus once: roughly **$5 in tokens**, plus whatever
you'd already pay for whisper.cpp (which is free, runs on your hardware).

Ongoing: maybe 1-2 new sessions a week, each agent-processed for ~$0.02.

Compared to OpenAI Whisper API ($0.006/min × 60min × 195 = $70.20 just
to *transcribe*), the local studio + agent layer is roughly **20x cheaper
end-to-end** and stays private.

## Fitting into a Claude ecosystem (Claude Desktop / Claude Code / Cowork)

You said you plan to use Claude as your agent platform with this as one of
the agents working for you. That's a cleaner mental model than "build a
custom agent harness" — the studio becomes a *capability* that any of
your Claude sessions can reach for, rather than a separate piece of
software you have to babysit.

Three points of integration, easiest to most powerful:

### 1. As an MCP server (best long-term)

Wrap the studio's HTTP routes in an MCP server (~200 lines of Python).
Add it to Claude Desktop and Claude Code's config. Now:

- **Claude Desktop**: "Hey Claude, summarize my last three Samvaad
  sessions." → Claude calls `transcribe-studio:list_transcripts`,
  reads them, summarizes. No copying files around.
- **Claude Code**: When you're working on a screenplay or article that
  references your therapy notes, Claude Code can read just the relevant
  transcripts via the MCP tools instead of you pasting them.
- **Cowork**: Same, with the file-system access already in place.

The studio becomes infrastructure. Multiple Claude conversations across
multiple apps all pull from the same canonical transcripts and the same
queue.

MCP tools to expose:
```
transcribe-studio:list_projects()
transcribe-studio:list_transcripts(project_id, since=, has_warnings=)
transcribe-studio:read_transcript(path, format=txt|srt)
transcribe-studio:get_status()
transcribe-studio:trigger_run(project_id, paths=[...])
transcribe-studio:retranscribe(path, no_context=true)
transcribe-studio:create_project(...)
transcribe-studio:add_to_project(project_id, folder=)
```

Implementation cost: a small Python file using
[fastmcp](https://github.com/jlowin/fastmcp) or the official MCP SDK.
Could ship with the studio in a day's work.

### 2. As a Cowork plugin

Cowork supports plugins. The studio could ship as
`transcribe-studio.plugin` containing:
- the MCP server
- a skill (`transcribe-studio/SKILL.md`) describing how to use it
- some commands (`/list-transcripts`, `/transcribe-folder`)

Anyone with the plugin installed gets the studio's capabilities inside
their Cowork session. You'd install once on this Mac, and your future
Cowork sessions automatically have transcript access without any setup.

### 3. As a Claude Code subagent

Claude Code supports subagents — specialized agent personalities you can
invoke. A `transcribe-agent` subagent could be:

- Goal: "Maintain clean, AI-ready transcripts across all my projects."
- Tools (via MCP): the list above
- Triggered: by `/transcribe` slash command, or proactively
  when a new file appears in a watched folder

You could then say in any Claude Code session: "Use the transcribe agent
to make sure last week's therapy sessions are processed and indexed."

### What this looks like in practice

Imagine a Sunday-morning routine where you check your week:

1. You open Claude Desktop and ask:
   "What were the recurring themes in my Samvaad transcripts this week?"

2. Claude (via the MCP server) calls `list_transcripts(project_id="samvaad", since="2026-04-25")`,
   gets back four transcripts.

3. Claude reads them, identifies themes, writes back to you in plain
   English.

4. You ask a follow-up: "Find the moment in session #142 where I talked
   about my mother."

5. Claude calls `read_transcript`, finds the timestamp via the .srt,
   gives you "00:34:12 — you said …".

The studio doesn't know any of this is happening. It's just the queue and
the transcripts. The intelligence lives in Claude. The integration lives
in MCP.

### Comparing this to a custom agent loop

The custom-agent path (option A in the previous section) builds a
dedicated, always-running agent. That's powerful but adds a new
long-running process to maintain.

The Claude-as-agent path keeps the studio focused on what it's good at
(deterministic transcription) and lets Claude be the brain *whenever you
want a brain* — no long-running agent process, no API costs when you're
not using it. It also means the agent gets smarter every time Claude
gets smarter, which happens roughly quarterly without any work from you.

For your usage pattern, this is almost certainly the right shape: studio
runs 24/7 doing the deterministic work, Claude works on top whenever you
ask it to.

## How an MCP server fits

There's a third pattern worth thinking about: expose the studio as an
**MCP server**. Then any Claude application — Claude Desktop, Claude
Code, custom agent harnesses — can talk to it as a tool provider.

MCP tools the studio could expose:
- `transcribe-studio:list_projects`
- `transcribe-studio:list_transcripts(project_id, since=, has_text=)`
- `transcribe-studio:read_transcript(path)`
- `transcribe-studio:trigger_run(project_id, paths=...)`
- `transcribe-studio:get_status`
- `transcribe-studio:create_project(...)`

You then have multiple specialized agents — a therapy-reflection agent, a
music-progress agent, a creative-writing agent — that all share the
transcription backbone. None of them re-implement whisper.cpp. None of
them re-implement the queue.

This is the configuration most likely to age well.

## Concrete next-step ladder

1. **Right now:** Studio works. Run it. Build up the corpus. Confirm
   the new transcripts come out clean.

2. **Once corpus is partially built:** wrap the studio's HTTP routes as
   an MCP server. ~200 lines of Python using `fastmcp`. Add it to your
   Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`).
   Test: open Claude Desktop, ask "summarize my last three Samvaad
   sessions." Claude calls the MCP tools directly. No agent needed —
   you ARE the agent at this stage, asking when you want answers.

3. **Then:** package the studio + MCP server as a Cowork plugin so you
   can install it from any Mac in one click and get the same toolset
   everywhere.

4. **Then:** specialized Claude Code subagents per project type
   (`transcribe-therapy-agent`, `transcribe-music-agent`). Each has its
   own prompt, knowledge of what to look for, and writes to a project's
   knowledge base. Invoke them via slash commands or have them run on a
   schedule.

5. **Eventually (only if you actually need it):** a long-running custom
   agent that nudges you proactively. By this point you'll know whether
   that's useful or annoying.

## The emotional truth

The studio is a tool. The agent is a *practice*. The studio does what you
tell it to. The agent notices things you'd miss. For a corpus that is
literally 4 years of therapy, the second one might be more important than
the first.

But you also don't want a babysitter. The agent has to be useful without
being intrusive — observant, not pestering. Get to know what your
intervention threshold is, then write that into the system prompt.

## Open questions to think about

- **Where does the agent live in the long run?** A daemon process on this
  Mac, or a small server somewhere?
- **Local LLM or Anthropic API?** Local saves money and keeps everything
  private; cloud is smarter and easier to update.
- **Should it write to your knowledge somewhere?** Notion, Obsidian, plain
  Markdown, Apple Notes? The choice affects retrieval.
- **How does it know what's important?** You'll have to teach it — or build
  the heuristic by example over a few weeks.
- **What level of agency?** Read-only (summarize, never modify)? Suggest
  changes (write to a "review queue" you approve)? Fully autonomous within
  a project (move files, change configs)?

These don't need answers yet. They become real once you've used the studio
for a few weeks and notice what's missing.
