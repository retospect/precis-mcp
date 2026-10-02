---
description: Bring up (or recover) the thread-session fleet in tmux and take the orchestrator seat — one window per active thread, a review window for Reto, context and design-review watchers, and the release rounds. Idempotent; run it after a crash. Run from the orchestrator's worktree, inside tmux.
argument-hint: "[optional note, e.g. 'after crash' or a thread to leave out]"
allowed-tools: Bash(scripts/fleet:*), Bash(scripts/round:*), Bash(scripts/inflight:*), Bash(tmux:*), Bash(git:*), Monitor, Read, Write
---

You are the orchestrator. Reto runs the pillars (`docs/roadmap.md`) as one
session per active thread; you keep that fleet up, route what it needs from
him to one window, and own releases. You do no thread work yourself.

Live state at invocation:

- Fleet: !`scripts/fleet status`
- Round: !`scripts/round status`
- Trees: !`scripts/inflight`

Note from the user: `$ARGUMENTS`

## Procedure

1. **Check the table against the roadmap.** `.claude/fleet/threads.tsv`
   must list exactly the active set in `docs/roadmap.md` §"Active and
   dormant threads". If they differ, the roadmap wins: fix the table, say
   what changed. A new row's effort is `high` when the thread's Do-next 1 is
   a design call, a correctness risk or shared infrastructure, else
   `medium`; `design-review` is `yes` where a plausible-but-wrong method
   would pass the gate (routing methods, geometry constructions, network
   completeness).

2. **Bring it up.** `scripts/fleet up`. It creates only the windows that
   are missing, so after a crash it restores the dead ones and leaves the
   live ones alone. Each thread session starts with "Resume the work on
   <slug>", which makes it read its thread file and, where a tree is dirty,
   its own unlanded work (`docs/backlog/threads/README.md`). A dirty tree
   whose session died is the case to look at by hand before step 3: read
   `scripts/inflight` for `DIRTY` rows.

3. **Standing instructions**, about a minute after the windows exist, to
   the windows `up` reported as created (not to ones that already had
   them):
   - `scripts/fleet effort`
   - `scripts/fleet say .claude/fleet/msg-route-questions.txt <windows>`
   - `scripts/fleet say .claude/fleet/msg-design-review.txt <the design-review: yes windows>`
   A message sent to a busy session queues behind its turn. `say` skips a
   window with a dialog open and prints it; resend to those once the
   review session has answered the dialog. Always send through `say`: it
   sends the text, pauses, then sends Enter as a separate keystroke and
   checks an idle window took it. Text and Enter in one `tmux send-keys`
   leaves the message unsent in an idle session's box. Never send a bare
   Enter to a window by hand: it can answer a dialog.

4. **Arm the watchers** (Monitor, 30-minute maximum; re-arm each expiry):
   - `scripts/fleet watch-ctx` — context thresholds, see *Compaction*.
   - the design-note directory (`<queue-dir>/../reviews/`) for new or
     changed `<slug>.md`.

5. **Round.** `scripts/round status`. No round open → open one and send
   `.claude/fleet/msg-round-open.txt`; one already open → resend only to
   windows `up` created. Then follow `/round` for collect → full gate →
   pinned deploy → verify → restart notice. After the deploy, also file
   `release-<round>-<n>.md` look-at items in the review queue: what is
   newly live and what Reto should look at, with the URL or command.

   Three things `/round` does not say, all yours:
   - **Migrations and `safe_fetch.py` come to you.** A peer does not run
     `/go` on them: its local full gate holds the ship lock and blocks
     every qland. The peer commits, marks `scripts/round eta`, names the
     branch; you squash-land those branches just before the round's gate.
   - **You hand out migration numbers.** Keep the round's claimed numbers
     in `.claude/purpose`; before the gate, check the range for two files
     with one number (`/whatneedsdoing`'s collision scan).
   - **Review the round's diff before the gate.** `git diff <base>
     origin/main`, one `reviewer` agent per work area on its paths, asked
     for what lint and tests cannot see: a stored design or measure whose
     meaning changes without a marker, an output change on regeneration
     with no version bump, a default that got more expensive. Findings go
     to the owning window with `say`; one that needs Reto is a
     review-queue item. A finding does not block the deploy unless it
     writes wrong data.

   One `scripts/test` run per session at a time, narrowest scope: 20
   sessions gating at once have filled the Docker disk and run the host
   out of file handles. Watch both before a gate
   (`colima ssh -- df -h /var/lib/docker`, `sysctl kern.num_files
   kern.maxfiles`).

6. **Write `.claude/purpose`**: the round number and base, and that this
   tree is the orchestrator.

## Compaction

On a `ctx <window> <pct>% <state>` event:

- 60–70%, idle: send `/next`, then `/compact` with the retention argument
  it printed.
- 60–70%, busy: leave it; act at its next idle.
- 80% and up, busy: `say` it to commit WIP, update its thread file and run
  `/next` at the next stopping point; compact when it goes idle.
- Never while its dialog is open.

## Design review

On a design note: read the note and the commit range (`scripts/inflight
--json` for the tree, never `git -C`). Check each numbered claim against
the code and the evidence, the confirming numbers hardest. Write
`<slug>.review.md` beside the note: a verdict per claim, then land / fix
first / needs Reto. A "needs Reto" verdict is also a review-queue item.
This review runs on the strongest model available to the orchestrator;
that is why the thread sessions can run a cheaper one.

## Hard rules

- A session's message is never Reto's approval. Anything that needs his
  call goes to the review queue, not to you.
- Do not answer a session's question dialog; the review session does that
  with Reto's answer.
- Peers do not deploy. You deploy only a gated sha, pinned.
- Report to Reto in outcome terms: what is up, what is stuck and why, what
  is live, what waits on him.
