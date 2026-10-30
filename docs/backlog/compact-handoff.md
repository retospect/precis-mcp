---
status: draft
title: /next writes a handoff memory node and a pointer; a SessionStart(compact) hook injects it — no copy-paste restart
pillar: operations
---

# /next → /compact handoff loop

2026-10-30: the primary handoff payload is the twin snapshot (context-navigation.md §7) rendered back as pointer rows; the `/next`-written node stays as the intent layer; hook mechanics here unchanged. Merge into the twin reader once it exists (thread context-navigation, Horizon).

Reto 2026-10-24 (chat, relayed by chat-9a): "lets do the /next /compact
loop with a hook for now". Reto rules on this spec before anyone builds.

Today `/next` (`.claude/commands/next.md`) prints two fenced blocks — a
`/compact Keep: …` argument and a recovery prompt — and Reto copies the
first, runs it, then pastes the second. Target: `/next` writes the handoff
into the repo-dev graph, prints `/compact resume from me<id>`, and a
SessionStart hook puts the node back into context after compaction.

## Why /compact, not /clear

| | `/compact` | `/clear` |
|---|---|---|
| session | same session; SessionStart fires with `source: "compact"` | ends the session: SessionEnd fires with `reason: "clear"`, then SessionStart with `source: "clear"` |
| background subagents, shells, Monitors | not documented as affected; the session that owns them continues | undocumented; issue reports conflict (killed: anthropics/claude-code#32052; survived: #44357, #26117) |
| session crons (CronCreate) | session-only jobs belong to the session, which continues | session-only jobs are "gone when Claude exits"; whether /clear counts is undocumented |
| hook output from background hooks | applies | "If you run `/clear` … while background hooks are still running, nothing they return applies to the session" (hooks reference) |
| custom text to hooks | `/compact <text>` reaches PreCompact as `custom_instructions` | none |

Sources: [hooks reference](https://code.claude.com/docs/en/hooks)
(SessionStart `source`, SessionEnd `reason`, PreCompact input),
[commands](https://code.claude.com/docs/en/commands) ("To free up context
while continuing the same conversation, use `/compact` instead").
`/compact` is the documented continue-the-session path; `/clear` support
(matcher `compact|clear`) waits until its effect on background work is
documented or measured.

## The loop

```
/next ──put──▶ memory me<id> (section:handoff, seat:<seat>)
      ──write▶ pointer ~/.cache/precis/handoff/<tree>.<pane>.json
      ──print▶ "/compact resume from me<id>"
Reto runs it ─▶ PreCompact(manual): custom_instructions "resume from me<id>"  [slice 4]
             ─▶ compaction
             ─▶ SessionStart(compact) ─▶ resolve id ─▶ fetch node ─▶ additionalContext
```

### Seat

A seat is the tmux window name of `$TMUX_PANE`
(`tmux display-message -p -t "$TMUX_PANE" '#W'`, e.g. `chat-99`), else the
tree name. `scripts/handoff-pointer seat` prints `seat=<s> tree=<t>
pane=<p|nopane>`.

### Node: `section:handoff`

Chosen over `section:threads`. A thread node is one per campaign, edited in
place; keeping N handoffs per seat is the resume-chain shape the thread
sweep exists to kill (`src/precis/workers/memory_thread_sweep.py` module
docstring: "four of them one session's resume chain"), and it trips
memory-lint check 10b (two live threads sharing a title prefix), check 9 and
the sweep's rule c. A handoff is session resume state for one compaction;
the campaign state stays in the seat's thread node.

| field | value |
|---|---|
| tags | `SPACE:repo-dev`, `section:handoff`, `seat:<seat>`, `tree:<tree>` |
| first body line (title) | `handoff <seat> <YYYY-MM-DDTHH:MMZ>` |
| body | Where / Goal / Done / In flight / Steps / Re-read / Watch out / Residue (template in the diff below); under the 4000-char fisheye cap; no line starting `Left:` or `NEXT` (memory-lint check 7 reads those as thread markers) |
| `meta.hook` | `<seat>: <step 1>` |
| links | `part-of` the handoffs hub; `related-to` the seat's `section:threads` node when one exists; `related-to` the seat's previous handoff |

The handoffs hub is one `section:index` memory titled `handoffs`, `part-of`
root me474312, created once by the slice 1 builder.

### Pointer

`${XDG_CACHE_HOME:-~/.cache}/precis/handoff/<tree>.<pane>.json`, `<pane>` =
`$TMUX_PANE` without `%`, or `nopane`:

```json
{"me": "me512345", "seat": "chat-99", "tree": "spec-handoff", "pane": "%12", "written_at": "2026-10-24T14:02:11Z"}
```

Written by temp file + rename (last `/next` wins). Holds the id and the
keys, never handoff text, so it is not an untracked resume file
(AGENTS.md §Repo-dev graph memory). Outside the repo: no gitignore entry,
and the worktree reaper never sees it.

### SessionStart(compact) resolution

`scripts/hooks/session-start-handoff.sh` reads stdin (`session_id`, `cwd`,
`source`), then:

1. Session marker `…/handoff/session.<session_id>.json` from the PreCompact
   hook (slice 4) if present; else the pane pointer for (tree of `cwd`,
   `$TMUX_PANE`).
2. Claim it: rename to `<name>.consumed`. A failed rename means another
   hook took it; print nothing.
3. Fetch the node with `scripts/prod-precis memory show me<id> --for-hook`
   (new CLI subcommand, slice 2; a shell hook cannot reach the session MCP,
   as `scripts/hooks/session-start-memory.sh` notes).
4. Check the node's `seat:` and `tree:` tags against the current seat and
   tree.
5. Emit `hookSpecificOutput.additionalContext`:
   `Handoff me<id> (<seat>, <age>) — resume from it:` + the body.

| case | output |
|---|---|
| no pointer, no marker | nothing (exit 0) |
| pointer older than 2 h | one line: `stale handoff me<id> (<age>) not injected; get(kind='memory', id='me<id>') if it is yours` |
| node retired or missing (CLI exit 3/4) | one line: `handoff me<id> is retired/missing; nothing injected` |
| seat or tree tag mismatch | one line naming both seats; nothing injected |
| graph unreachable (CLI non-zero otherwise, or over 15 s) | one line: `graph unreachable; handoff me<id> not injected — get(kind='memory', id='me<id>') when the MCP is back`, plus the body from `~/.cache/precis/memory-nodes/<handle>.md` when a startup export already holds it, labelled as that cached copy |
| auto-compact marker, no handoff (slice 4) | one line: `auto-compacted without /next; latest handoff for <seat>: me<id> (<age>)` or `… none` |

Never blocks: every failure path exits 0.

### Outage

MCP down at `/next` time: the put fails, `/next` writes no pointer and no
`outage-memory.md` entry (the outage file is for durable facts, not resume
state), and prints the body as one fenced block plus "Run /compact, then
paste the block as your first message" — the current behaviour, one block.

### Two panes in one tree

Pane keys differ, so pointers do not collide. Shared key cases: two
sessions outside tmux in one tree (both `nopane`), or a new `claude` in the
same pane picking up the last one's pointer. Defences, in order: the slice 4
session marker (exact: `custom_instructions` carries the id and the
payload carries `session_id`); consume-on-read; the 2 h staleness cut; the
seat/tree tag check.

## Fallback: PreCompact (slice 4)

`scripts/hooks/precompact-handoff.sh`, stdin `trigger`,
`custom_instructions`, `session_id`:

- `manual` and `custom_instructions` matches `resume from (me[0-9]+)` →
  write the session marker with that id.
- otherwise (`auto`, or `/compact` without an id) → write the session
  marker with `"me": null`, so SessionStart prints the auto-compact line.
- File writes only: no network, no MCP, no git, no lint. Budget under
  200 ms; `"timeout": 5` in settings. Exit 0 always: PreCompact exit 2 or
  `decision: "block"` blocks compaction (hooks reference), and a failed
  context-limit recovery would fail the request.

Coexistence with `scripts/hooks/precompact-persist.sh`: a second entry in
the same PreCompact hook list. The two share no files and neither reads the
other's output, so their run order does not matter. precompact-persist.sh
keeps its reminder and lint lines unchanged; its memory-lint and
backlog-lint calls are why the handoff hook does not live inside it.

Later slice (not specced here): an `async: true` summarizer that reads
`transcript_path` after an auto-compact with no `/next`, masks credentials
with `src/precis/utils/secret_scan.py::mask_secrets`, and writes a handoff
node itself.

## Ageing (slice 3)

Runs in the daily `memory_thread_sweep` cadence
(`src/precis/workers/scheduler.py::_run_memory_thread_sweep`) as a new rule
in `src/precis/workers/memory_thread_sweep.py`: same soft-delete
(`Store.retire_ref`), same log line, same `precis memory sweep` dry run.
Not a hook (sessions would race each other) and not the reconsolidate skill
(runs only when a session acts on its nag).

**rule h**, over live `section:handoff` memories grouped by `seat:` tag,
newest `updated_at` first: retire when rank > 3 or age > 7 days, unless a
live `section:threads` memory is the source of a link to it (a thread
pinning a handoff on purpose). A handoff's own outbound `related-to` to its
thread does not exempt it. No todo is minted: open work lives in the thread
node's `Left:` line and the linked todos.

```
memory_thread_sweep: retired me<id> rule=h — seat chat-99 rank 4
```

memory-lint stays quiet when:

- `src/precis/utils/memory_hubs.py::SECTION_TYPES` gains `handoff`;
  `src/precis/cli/memory.py::_TYPE_PREFIX` gains `[handoff]`.
- `precis memory index` leaves `section:handoff` nodes out of the rendered
  list and counts (they are reached by pointer, not by the index).
- `scripts/memory-lint` checks 2, 7, 9 and 10 skip `section:handoff` by tag.
- The big-hub check (> 40 direct members) skips the handoffs hub: rule h
  bounds it at 3 × seats. Open question 3.

## Slices

| # | what | size | depends on |
|---|---|---|---|
| 1 | `scripts/handoff-pointer` (`seat`, `write`), the handoffs hub, the `/next` diff (Reto applies) | 1 build | — |
| 2 | `precis memory show --for-hook`, `scripts/hooks/session-start-handoff.sh`, settings block A (Reto applies) | 1 build | 1 (pointer format; can build in parallel against this spec) |
| 3 | rule h, `SECTION_TYPES`/`_TYPE_PREFIX`, index exclusion, memory-lint skips; AGENTS.md §Repo-dev graph memory and the `remember` skill name `section:handoff` | 1–2 builds | 1 (tag shape); must land before the hub passes 40 members |
| 4 | `scripts/hooks/precompact-handoff.sh`, session-marker read in the slice 2 hook, settings block B (Reto applies) | 1 build | 2 |

Builders of every slice never edit `.claude/settings*.json`,
`.claude/commands/`, `.claude/agents/` or any hooks wiring. `next.md` lives
under `.claude/commands/`, so the slice 1 change to it is Reto-only: the
builder substitutes the hub id into the diff below and hands it to Reto.

### Acceptance

1. `tests/test_handoff_pointer.py`: `write` produces the JSON above
   atomically (a reader during a concurrent write sees the old or the new
   file, never a partial one); `seat` falls back to the tree name with
   `TMUX_PANE` unset and `nopane` in the file name; `write` refuses an id
   not matching `me[0-9]+`. Manual: after the diff is applied, `/next` in a
   test seat prints exactly one line and the node shows the hub, the thread
   link and the previous-handoff link in `get(…, view='fisheye+1hop')`.
2. `tests/test_session_start_handoff_hook.py`, CLI stubbed through a
   `PRECIS_MEMORY_CLI`-style seam: one test per row of the resolution table;
   a second run after a successful inject prints nothing (consumed);
   two concurrent runs inject once; output is valid
   `hookSpecificOutput` JSON; wall time with the stub under 1 s.
   `tests/test_memory_cli.py` (or the existing CLI test file): `show`
   exits 3 on a retired ref, 4 on a missing one, and caps the body at
   4000 chars.
3. `tests/workers/test_memory_thread_sweep.py`: rank 4 retires, rank 3
   under 7 days stays, rank 1 over 7 days retires, a thread-sourced link
   exempts, an outbound handoff→thread link does not, dry run writes
   nothing, `section:threads` rules a–c unchanged on their fixtures.
   `tests/test_memory_lint.py`: a manifest with handoff nodes (shared title
   prefix, 45 hub members) yields no findings; the same nodes tagged
   `section:threads` still yield checks 9/10. `tests/test_memory_index_retired.py`
   or a sibling: index output omits handoff nodes.
4. `tests/test_precompact_handoff_hook.py`: manual with
   `resume from me123` writes marker `me123`; manual without an id and auto
   write `null`; malformed stdin exits 0 and writes nothing; under 200 ms;
   the slice 2 hook prefers the session marker over a pane pointer naming a
   different id.

## Settings blocks (Reto applies)

A — slice 2: append to the `"SessionStart"` array, after the existing
unmatched group:

```json
{
  "matcher": "compact",
  "hooks": [
    {
      "type": "command",
      "command": "bash \"$CLAUDE_PROJECT_DIR/scripts/hooks/session-start-handoff.sh\"",
      "timeout": 20
    }
  ]
}
```

B — slice 4: append to `"PreCompact"[0].hooks`, after
`precompact-persist.sh`:

```json
{
  "type": "command",
  "command": "bash \"$CLAUDE_PROJECT_DIR/scripts/hooks/precompact-handoff.sh\"",
  "timeout": 5
}
```

`/clear` later: change A's matcher to `"compact|clear"`.

The existing unmatched SessionStart group (13 hooks) also fires on every
compaction; this spec does not change that.

## `/next` diff (Reto applies with slice 1)

`me<HUB>` is the handoffs hub id the slice 1 builder fills in.

`````diff
diff --git a/.claude/commands/next.md b/.claude/commands/next.md
--- a/.claude/commands/next.md
+++ b/.claude/commands/next.md
@@ -1,39 +1,35 @@
 ---
-description: "Summarize next steps; emit a /compact retention argument and a copy-paste recovery prompt. Use before compacting or for a clean restart point."
+description: "Write this seat's handoff node to the repo-dev graph and print one `/compact resume from me<id>` line. Use before compacting or for a clean restart point."
 argument-hint: "[optional steer — where you want to go next, e.g. 'do the factory with the flubber']"
-allowed-tools: Bash(git:*), Read, Glob, Grep, mcp__precis__get, mcp__precis__search
+allowed-tools: Bash(git:*), Bash(date:*), Bash(scripts/handoff-pointer:*), Read, Glob, Grep, mcp__precis__get, mcp__precis__search, mcp__precis__put, mcp__precis__link
 ---
 
-You cannot run `/compact` yourself. Your job is to hand the user a **clean
-restart** via two complementary artifacts, split by **durability**:
+You cannot run `/compact` yourself. Your job is to leave a **handoff node**
+in the repo-dev graph that the next context reads, and to print the one
+line the user runs. The SessionStart hook (`compact` matcher,
+`scripts/hooks/session-start-handoff.sh`) injects the node after
+compaction; nobody copies or pastes a block. Spec:
+`docs/backlog/compact-handoff.md`.
 
-- a **`/compact` retention argument** that steers the summarization itself —
-  its only job is to protect the **ephemeral working context that has no
-  durable home** (the reasoning compaction would otherwise destroy);
-- a self-contained **recovery prompt** they paste as their first message
-  afterward, which re-grounds from **durable artifacts** on disk.
-
-The split: the recovery prompt says *where to look*; the compact argument
-keeps *what you can't look up*. So the compact argument must NOT restate
-Where/Goal/Next-steps (the recovery prompt carries those from durable
-artifacts) — it carries only the transcript-only residue that survives
-nowhere else.
+The node holds two things: **where to look** (pointers to durable
+artifacts) and **what you can't look up** (transcript-only residue:
+decisions, rejected alternatives, verification state).
 
 **Corollary — anything you wrote to a file this session is, by that act,
 durable: reference it by locator and stop.** Do not restate its content in
-either block, and specifically do not do it framed as "here's what I saved,
+the node, and specifically do not do it framed as "here's what I saved,
 don't re-carry it" — narrating a fact in order to tell the next session not
 to carry it *is* carrying it, at full token cost. If a claim now lives in
 `docs/backlog/`, a `todo`/`gripe`, a docstring, or any on-disk note, the
 pointer (slug / id / path) is the entire job; the bytes are on disk. This is
-the single most common way both blocks bloat — one durable locator replaces
+the single most common way a handoff bloats — one durable locator replaces
 a paragraph.
 
 **The live state below only covers this repo.** A clean/empty git status does
 not mean the session did nothing — durable output can land entirely outside
 this worktree's git tree: graph memory (`kind='memory'`, `SPACE:repo-dev`;
 AGENTS.md §Repo-dev graph memory), or precis MCP writes to
-`kind='todo'`/`gripe`. A recovery prompt cites meIDs and recall queries,
+`kind='todo'`/`gripe`. A handoff cites meIDs and recall queries,
 not memory file paths. If that's where this session's work actually went, say
 so plainly and point at it (path or id) — don't read an empty git diff as
 "nothing to hand off," and don't let it read as a ship failure either (that's
@@ -47,6 +43,8 @@ Live state at invocation:
   !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(nothing ahead / can't compute)"`
 - Uncommitted changes:
   !`git -c color.ui=never diff --stat HEAD 2>/dev/null | tail -20`
+- Seat:
+  !`scripts/handoff-pointer seat`
 
 Optional steer from the user: `$ARGUMENTS`
 
@@ -61,17 +59,15 @@ it, then let it shape the whole handoff:
   `precis get`/`search`) to pin it down. If you genuinely can't map it to
   anything, say so and ask rather than inventing a target.
 - **Let it bias what's kept and what's queued.** The steer sets the destination;
-  the recovery prompt's Goal + Next-steps should point there, and the compact
-  retention argument should preferentially preserve the transcript-only residue
-  that serves *that* direction (and can drop residue for threads the user is
-  clearly setting aside). Don't discard genuinely-open work the steer ignores —
-  note it as a parked pointer — but the ordering and emphasis follow the steer.
+  the node's Goal + Steps should point there, and its Residue should
+  preferentially keep what serves *that* direction (and can drop residue for
+  threads the user is clearly setting aside). Don't discard genuinely-open work
+  the steer ignores — note it as a parked pointer — but the ordering and
+  emphasis follow the steer.
 
 ## Procedure
 
-0. **Stamp the time.** Run `date -u` first and lead the output with a line like
-   `Generated at 22:31 UTC`, above the compact argument. Reto reads it on a stale
-   tab to choose `/compact` (cache warm) over `/clear` plus the recovery block.
+0. **Stamp the time.** Run `date -u`; the node title carries it.
 
 1. **Establish the next logical steps.** From the work this session, name what
    comes next concretely — the in-progress edit to finish, the next item on a
@@ -90,13 +86,13 @@ it, then let it shape the whole handoff:
    landed commit fixed, a `docs/backlog/` item whose fix has merged), close
    it now — `resolved: <sha> …` comment plus `STATUS:done` for a gripe, delete-the-file
    for `docs/backlog/` (per `/whatneedsdoing`'s convention) — rather than
-   leaving it dangling for the recovery prompt to route around. Don't
+   leaving it dangling for the handoff to route around. Don't
    persist things that don't need it: a next step you're about to hand off
-   in the recovery prompt anyway, and that only this session need act on, can
+   in the node anyway, and that only this session need act on, can
    stay there — this step is for what would otherwise be lost.
 
-3. **Anchor to durable artifacts.** The recovery prompt must point at things
-   that survive compaction, not at "as we discussed": the worktree path +
+3. **Anchor to durable artifacts.** The node must point at things
+   that survive compaction, not at "as we discussed": the worktree name +
    branch, the files touched, the relevant `docs/backlog/` item, any
    `kind='todo'` / `gripe` ids, and — when the work belongs to a thread —
    the thread file (`docs/backlog/threads/<slug>.md`) plus its line in
@@ -107,50 +103,39 @@ it, then let it shape the whole handoff:
    isn't persisted anywhere durable and matters, note that gap to the user (they
    may want it in `docs/backlog/` or a todo before compacting).
 
-   Emit the two blocks **in the order the user runs them**: the `/compact`
-   retention argument first, then the recovery prompt.
-
-4. **Emit the compact retention argument.** Output the first fenced block: a
-   `/compact` invocation whose argument protects the un-persisted reasoning —
-   everything the recovery prompt's pointers can't reconstruct. Source it from
-   the "Watch out" / gap notes above: anything there NOT backed by a file
-   belongs here. Do not duplicate Where/Goal/Next-steps. **Test each candidate
-   fact against "is this already on disk?"** — if you persisted it this session
-   (or it was already in a file), it fails the test: drop it entirely, don't
-   convert it into a "don't re-carry X" reminder that spells X out. Template:
-
-   ````
-   /compact Keep: <decisions made this session and the alternatives rejected + why; constraints/gotchas discovered but not yet written to any file; current verification state (what's been run and passed vs. untested)>. Preserve branch/worktree + todo/gripe ids verbatim. Drop tool-output dumps, file contents (re-readable from disk), and resolved dead-ends.
-   ````
-
-   If genuinely nothing lives only in the transcript — every open thread is
-   already durable — say so and skip this block rather than padding it.
-
-5. **Emit the recovery prompt.** Output the second fenced block the user can copy
-   verbatim. Fill every bracket from real state — no placeholders left in. Keep
-   it tight; it is a re-orientation, not a transcript replay. Template:
+4. **Write the handoff node.** Find this seat's previous handoff:
+   `search(kind='memory', tags=['SPACE:repo-dev', 'section:handoff', 'seat:<seat>'], view='index')`.
+   Then `put(kind='memory', tags=['SPACE:repo-dev', 'section:handoff', 'seat:<seat>', 'tree:<tree>'], meta={'hook': '<seat>: <step 1>'}, text=…)`
+   with this body, every bracket filled from real state, under 4000 chars:
 
    ````
-   Resuming after /compact. Reorient, then continue.
-
-   **Where:** worktree `<path>` on branch `<branch>` (<N ahead / clean / dirty>); thread `<slug>` (<active|dormant>, pillar <n> per docs/roadmap.md) — or "no thread".
-   **Goal:** <one-sentence what-we're-driving-at>.
-   **Done so far:** <2–4 bullets of what's landed/decided this session>.
-   **In flight:** <what's half-done right now, if anything — file:line if precise>.
-
-   **Next logical steps:**
+   handoff <seat> <YYYY-MM-DDTHH:MMZ>
+   Where: tree `<tree>` on branch `<branch>` (<N ahead / clean / dirty>); thread `<slug>` (<active|dormant>, pillar <p>) — or "no thread".
+   Goal: <one sentence>.
+   Done: <2–4 bullets: shas, ids, paths — no content>.
+   In flight: <path::Symbol of the half-done edit, or none>.
+   Steps:
    1. <concrete step>
-   2. <concrete step>
-   3. <…>
+   2. <…>
+   Re-read: <paths / backlog item / me·gr·td ids>.
+   Watch out: <gotchas, gate quirks, sibling trees — only what no file holds>.
+   Residue: <decisions + rejected alternatives + why; what has been run and passed vs. untested>.
+   ````
 
-   **Re-read to reground:** <files / docs/backlog/ item / todo ids / docs>.
-   **Watch out:** <any gotcha, gate quirk, or in-flight sibling worktree that bites>.
+   No absolute paths (tree names and repo-relative paths only), no tool
+   output, file contents, env values or credentials. Then link it:
+   `part-of` the handoffs hub me<HUB>,
+   `related-to` the thread's `section:threads` node when one exists, and
+   `related-to` the previous handoff.
 
-   Start by reading the "Re-read to reground" pointers, then do step 1.
-   ````
+5. **Write the pointer.** `scripts/handoff-pointer write me<id>`.
 
-6. **Close with the nudge.** After the blocks, one line:
+6. **Print exactly one line** and nothing after it:
    ```
-   Copy the /compact line → run it → paste the recovery block as your first message.
+   /compact resume from me<id>
    ```
-   Skip the whole thing only when the session was short and nothing is open.
+
+**Graph unreachable or the put refused** (MCP down — the outage rule in
+MEMORY.md): write no pointer and no outage-memory.md entry; instead print
+the node body above as one fenced block, then the line
+`Run /compact, then paste the block as your first message.`
`````

## Dogfood: chat-99 first

After slices 1 and 2 land and blocks A and the diff are applied, Reto runs
`/next` then the printed line in chat-99 at least three times over a day,
once with a background Monitor or subagent running. Success:

- zero pastes: the first post-compact turn names the node's step 1 and
  starts on it without being told;
- the Monitor or subagent started before `/compact` still delivers its
  notification after;
- the injected node is no larger than the two blocks it replaces
  (compare against the last old-style `/next` output in chat-99);
- `scripts/memory-lint` shows no new finding;
- no wrong-seat inject; hook time in the debug log
  (`Hooks: SessionStart:compact … finished … (<ms>)`) under 5 s.

Then slice 4, then the other seats.

## Risks

| risk | mitigation |
|---|---|
| secrets in handoff text (the graph is prod data, recalled into other sessions) | `/next` forbids tool output, file contents, env values and credentials in the body; memory put/edit already run `mask_secrets` (`src/precis/handlers/memory.py`) and dispatch runs `refuse_secrets` (`src/precis/runtime/dispatch.py`); the slice 4 summarizer masks before it writes |
| home paths in bodies (memory-lint check 11) | template uses tree names and repo-relative paths |
| pointer race (two `/next` in one pane, two sessions on one key) | rename-on-write, consume-by-rename, 2 h cut, slice 4 session marker |
| wrong-seat pickup | seat/tree tag check before inject |
| recall noise: handoffs in `search(kind='memory')` | rule h bounds the live set; revisit if dogfood shows handoffs crowding recall |
| hook latency on every compaction | `prod-precis` round trip only when a pointer exists; 15 s internal cut, 20 s timeout |

## Decisions (Reto 2026-10-24, chat-99)

- **Q1–Q4, Q6:** as recommended.
- **Q5 stale cut: not 2 h.** "If it makes a handoff at 2am and I get to it
  at 8am it ought to still work." The cut must survive an overnight gap.
  The chat desk proposes: with slice 4's session marker, an exact session
  match is honoured at any age; a pane-pointer-only match is honoured up to
  24 h; older, the hook names the id and injects nothing. The builder
  confirms this keeps the wrong-session risk acceptable.

## Open questions for Reto

1. **Section: `section:handoff` (new) or `section:threads`?** Recommend
   `section:handoff`: keep-3-per-seat contradicts the one-node-per-campaign
   thread rule and its lint and sweep.
2. **Ageing rule: retire at rank > 3 or age > 7 days, whichever first** (so
   an idle seat's last handoff also goes after 7 days)? Recommend yes; the
   seat's thread node carries the longer-lived state.
3. **Handoffs hub over 40 members:** exempt it from the big-hub check
   (recommended, size bounded by rule h) or one sub-hub per seat?
4. **Ship slice 4 with slice 2?** Recommend yes: the session marker is the
   only exact defence against shared pointer keys, and the hook is small.
5. **Stale cut 2 h?** Recommend 2 h; `/next` and `/compact` normally run
   minutes apart.
6. **`/clear` support:** add `clear` to matcher A after a measured check
   that background subagents and Monitors survive `/clear`, or never?
   Recommend measure in chat-99 once, then decide.
