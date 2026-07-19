---
description: Summarize the next logical steps and emit both a /compact retention argument and a copy-paste recovery prompt to resume after compacting. Use before compacting, or any time you want a clean-context restart point.
argument-hint: "[optional focus — what to center the handoff on]"
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

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(nothing ahead / can't compute)"`
- Uncommitted changes:
  !`git -c color.ui=never diff --stat HEAD 2>/dev/null | tail -20`

Optional focus from the user: `$ARGUMENTS`

## Procedure

1. **Establish the next logical steps.** From the work this session, name what
   comes next concretely — the in-progress edit to finish, the next item on a
   tracked list, the test to make green, the thing to verify. If `$ARGUMENTS`
   names a focus, center on it. If genuinely nothing is open, say so and stop —
   don't manufacture ceremony.

2. **Anchor to durable artifacts.** The recovery prompt must point at things
   that survive compaction, not at "as we discussed": the worktree path +
   branch, the files touched, the relevant `OPEN-ITEMS.md` section, any
   `kind='todo'` / `gripe` ids, the design doc or ADR in play. If a next step
   isn't persisted anywhere durable and matters, note that gap to the user (they
   may want it in `OPEN-ITEMS.md` or a todo before compacting).

3. **Emit the recovery prompt.** Output a single fenced block the user can copy
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

4. **Emit the compact retention argument.** Output a second fenced block: a
   `/compact` invocation whose argument protects the un-persisted reasoning —
   everything the recovery prompt's pointers can't reconstruct. Source it from
   the "Watch out" / gap notes above: anything there NOT backed by a file
   belongs here. Do not duplicate Where/Goal/Next-steps. Template:

   ````
   /compact Keep: <decisions made this session and the alternatives rejected + why; constraints/gotchas discovered but not yet written to any file; current verification state (what's been run and passed vs. untested)>. Preserve branch/worktree + todo/gripe ids verbatim. Drop tool-output dumps, file contents (re-readable from disk), and resolved dead-ends.
   ````

   If genuinely nothing lives only in the transcript — every open thread is
   already durable — say so and skip this block rather than padding it.

5. **Close with the nudge.** After the blocks, one line:
   ```
   Copy the /compact line → run it → paste the recovery block as your first message.
   ```
   Skip the whole thing only when the session was short and nothing is open.
