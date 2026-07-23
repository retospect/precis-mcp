---
description: Summarize the next logical steps and emit both a /compact retention argument and a copy-paste recovery prompt to resume after compacting. Use before compacting, or any time you want a clean-context restart point.
argument-hint: "[optional steer — where you want to go next, e.g. 'do the factory with the flubber']"
allowed-tools: Bash(git:*), Read, Glob, Grep, mcp__precis__get, mcp__precis__search
---

You cannot run `/compact` yourself. Your job is to hand the user a **clean
restart** via two complementary artifacts, split by **durability**:

- a **`/compact` retention argument** that steers the summarization itself —
  its only job is to protect the **ephemeral working context that has no
  durable home** (the reasoning compaction would otherwise destroy);
- a self-contained **recovery prompt** they paste as their first message
  afterward, which re-grounds from **durable artifacts** on disk.

The split: the recovery prompt says *where to look*; the compact argument
keeps *what you can't look up*. So the compact argument must NOT restate
Where/Goal/Next-steps (the recovery prompt carries those from durable
artifacts) — it carries only the transcript-only residue that survives
nowhere else.

**Corollary — anything you wrote to a file this session is, by that act,
durable: reference it by locator and stop.** Do not restate its content in
either block, and specifically do not do it framed as "here's what I saved,
don't re-carry it" — narrating a fact in order to tell the next session not
to carry it *is* carrying it, at full token cost. If a claim now lives in
`OPEN-ITEMS.md`, a `todo`/`gripe`, an ADR, or any on-disk note, the pointer
(`§section` / id / path) is the entire job; the bytes are on disk. This is
the single most common way both blocks bloat — one durable locator replaces
a paragraph.

**The live state below only covers this repo.** A clean/empty git status does
not mean the session did nothing — durable output can land entirely outside
this worktree's git tree: this Claude Code install's personal auto-memory
under `~/.claude/projects/<encoded-workdir>/memory/`, or precis MCP writes to
`kind='todo'`/`gripe`. If that's where this session's work actually went, say
so plainly and point at it (path or id) — don't read an empty git diff as
"nothing to hand off," and don't let it read as a ship failure either (that's
`/land`'s call, not this one).

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(nothing ahead / can't compute)"`
- Uncommitted changes:
  !`git -c color.ui=never diff --stat HEAD 2>/dev/null | tail -20`

Optional steer from the user: `$ARGUMENTS`

**If `$ARGUMENTS` is non-empty, treat it as a forward-intent steer, not just a
topic label.** It tells you where the user wants the *next* session to go — read
it, then let it shape the whole handoff:

- **Interpret it against real state first.** Resolve vague or playful phrasing
  ("do the factory with the flubber") to concrete artifacts before you write
  anything — the matching worktree, `OPEN-ITEMS.md` §section, `kind='todo'` /
  memory thread, design doc or ADR. Use the tools you have (git, Read/Grep,
  `precis get`/`search`) to pin it down. If you genuinely can't map it to
  anything, say so and ask rather than inventing a target.
- **Let it bias what's kept and what's queued.** The steer sets the destination;
  the recovery prompt's Goal + Next-steps should point there, and the compact
  retention argument should preferentially preserve the transcript-only residue
  that serves *that* direction (and can drop residue for threads the user is
  clearly setting aside). Don't discard genuinely-open work the steer ignores —
  note it as a parked pointer — but the ordering and emphasis follow the steer.

## Procedure

1. **Establish the next logical steps.** From the work this session, name what
   comes next concretely — the in-progress edit to finish, the next item on a
   tracked list, the test to make green, the thing to verify. If `$ARGUMENTS`
   names a steer, resolve it per the note above and center the next steps on
   where it points. If genuinely nothing is open, say so and stop — don't
   manufacture ceremony.

2. **Persist found issues, close fixed ones — same discipline as land/ship/go.**
   Before anchoring the handoff to durable artifacts, make them actually
   durable. Anything this session surfaced but never wrote down — a residual
   bug, a daemon left down, a design gap, a diagnosis not yet re-verified —
   gets a `kind='todo'` / `gripe` row or an `OPEN-ITEMS.md` entry now, the same
   "persist first" move `/go` step 8 makes after a ship. Conversely, if this
   session's work already resolved something durable-list-worthy (a gripe a
   landed commit fixed, an `OPEN-ITEMS.md` bullet whose fix has merged), close
   it now — resolution-comment-then-soft-delete for a gripe, delete-the-entry
   for `OPEN-ITEMS.md` (per `/whatneedsdoing`'s convention) — rather than
   leaving it dangling for the recovery prompt to route around. Don't
   persist things that don't need it: a next step you're about to hand off
   in the recovery prompt anyway, and that only this session need act on, can
   stay there — this step is for what would otherwise be lost.

3. **Anchor to durable artifacts.** The recovery prompt must point at things
   that survive compaction, not at "as we discussed": the worktree path +
   branch, the files touched, the relevant `OPEN-ITEMS.md` section, any
   `kind='todo'` / `gripe` ids, the design doc or ADR in play. If a next step
   isn't persisted anywhere durable and matters, note that gap to the user (they
   may want it in `OPEN-ITEMS.md` or a todo before compacting).

   Emit the two blocks **in the order the user runs them**: the `/compact`
   retention argument first, then the recovery prompt.

4. **Emit the compact retention argument.** Output the first fenced block: a
   `/compact` invocation whose argument protects the un-persisted reasoning —
   everything the recovery prompt's pointers can't reconstruct. Source it from
   the "Watch out" / gap notes above: anything there NOT backed by a file
   belongs here. Do not duplicate Where/Goal/Next-steps. **Test each candidate
   fact against "is this already on disk?"** — if you persisted it this session
   (or it was already in a file), it fails the test: drop it entirely, don't
   convert it into a "don't re-carry X" reminder that spells X out. Template:

   ````
   /compact Keep: <decisions made this session and the alternatives rejected + why; constraints/gotchas discovered but not yet written to any file; current verification state (what's been run and passed vs. untested)>. Preserve branch/worktree + todo/gripe ids verbatim. Drop tool-output dumps, file contents (re-readable from disk), and resolved dead-ends.
   ````

   If genuinely nothing lives only in the transcript — every open thread is
   already durable — say so and skip this block rather than padding it.

5. **Emit the recovery prompt.** Output the second fenced block the user can copy
   verbatim. Fill every bracket from real state — no placeholders left in. Keep
   it tight; it is a re-orientation, not a transcript replay. Template:

   ````
   Resuming after /compact. Reorient, then continue.

   **Where:** worktree `<path>` on branch `<branch>` (<N ahead / clean / dirty>).
   **Goal:** <one-sentence what-we're-driving-at>.
   **Done so far:** <2–4 bullets of what's landed/decided this session>.
   **In flight:** <what's half-done right now, if anything — file:line if precise>.

   **Next logical steps:**
   1. <concrete step>
   2. <concrete step>
   3. <…>

   **Re-read to reground:** <files / OPEN-ITEMS.md §section / todo ids / ADR / docs>.
   **Watch out:** <any gotcha, gate quirk, or in-flight sibling worktree that bites>.

   Start by reading the "Re-read to reground" pointers, then do step 1.
   ````

6. **Close with the nudge.** After the blocks, one line:
   ```
   Copy the /compact line → run it → paste the recovery block as your first message.
   ```
   Skip the whole thing only when the session was short and nothing is open.
