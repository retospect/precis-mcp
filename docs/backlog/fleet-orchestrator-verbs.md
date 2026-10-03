---
status: ready
title: scripts/fleet verbs for the orchestrator's repeated hand sequences (verdict, say -m/--when-clear, peek, compact-at-idle, mcp-check, one watcher)
pillar: platform
prio: high
---

# `scripts/fleet` verbs for the orchestrator's repeated hand sequences

Reto, 2026-10-03: "look at your transcripts and see if you need tools".
Source: a mining pass over the orchestrator's 24 h transcript
(`~/.claude/projects/-Users-reto-precis-mcp/scratch/organizer/mine-transcript.py`,
~1,150 tool calls): 102 verdict appends by `cat >> *.review.md <<EOF` with
135 hand-typed `HH:MMZ` stamps against 18 `date -u` calls (several stamps
were wrong by 1–2 h); 123 `scripts/fleet say` calls needing ~25 scratch
message files, 74 second-Enter retries and 19 "SKIPPED (dialog open)" that
each needed a hand-rolled waiter; 64 Monitor arms (two watchers × 30-min
expiry) producing ~160 notifications; 22 `tmux capture-pane` → `send-keys`
pairs; 14 harness refusals of loops/`cd`/cross-tree git that each became a
one-off script in scratch (76 of 99 Writes). Each verb below removes one of
those.

## In scope

1. `fleet verdict <slug> [--file f | -]` — append to
   `<reviews>/<slug>.review.md` with a `## … (orchestrator, <date -u> Z)`
   heading written by the tool, then `say` the standard "verdict written"
   message to the thread's window. No hand stamps ever again.
2. `fleet say -m "<text>" <win…>` (inline text, no file) and
   `fleet say --when-clear` (if the window has a dialog open, hold the
   message in `<git-common-dir>/precis-fleet/queue/<win>` and deliver when
   the dialog clears; `fleet queue` lists held messages). `say` keeps its
   dialog check and the separate Enter.
3. `fleet peek <win> [-n N]` — the window's last N non-empty pane lines,
   with the open dialog's full command text when one is showing; and
   `fleet dialogs` — every window with a dialog, its prompt type (hook /
   permission / question) and the command's first line, for Reto's review
   window.
4. `fleet compact <win…> [--at-idle]` — `/next` then `/compact` when the
   window is idle, refusing while busy or in a dialog; `--at-idle` waits.
5. `fleet mcp-check [--fix]` — per window, the newest
   `~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-{precis,claude-context}`
   state (connected / session-expired / "giving up" / stdio child gone),
   and with `--fix` sends `/mcp reconnect all` to idle windows that need it
   (from `scratch/organizer/mcp-log-summary.sh`, memory
   `mcp-http-reconnect-budget`).
6. `fleet watch` — ONE long-lived watcher emitting typed lines:
   `ctx <win> <pct> <state>` (today's watch-ctx), `note <slug>` (design
   note new/changed), `dialog <win> <type>` (dialog appeared),
   `ci main <sha> <conclusion>` (a completed check.yml run on main),
   `mcp <win> <state>` (a window's MCP went dead). Never exits on its
   own; the orchestrator re-arms on expiry only.
7. `fleet refs` — `origin/main`, `origin/gated`, `origin/prod` short shas
   with ages and the newest green main sha (`last-gated-main-sha`), one
   line each — the branch-state check the harness refuses as a loop.

## Explicitly NOT in scope

- Answering any dialog from a verb (`peek`/`dialogs` only read; clearing
  stays a human or an explicitly allowed orchestrator keystroke).
- Driving the orchestrator's own window (classifier: "Tmux Self Drive").
- Round verbs (`docs/backlog/release-candidate-verdicts.md`).

## Acceptance criteria

- `fleet verdict` output heading carries a `date -u` stamp within 1 s of
  the call; the thread window receives the notice (idle window: message
  visible in its last turn).
- `fleet say -m` to a window with a dialog returns `HELD`; after the
  dialog is answered the message arrives within 30 s without a further
  call; `fleet queue` showed it meanwhile.
- `fleet compact --at-idle` never sends while the window is busy or in a
  dialog (test against a window running `sleep 120` via its `!` prompt).
- `fleet mcp-check` on a window whose log ends in "giving up" reports
  `dead`; after `--fix` the window's log shows "Reconnected" and the state
  reads `connected`.
- `fleet watch` runs 2 h under the Monitor tool with one arm and emits
  every type above at least once in a test (synthetic: touch a note,
  open a dialog, complete a CI run).
- All verbs are plain commands the worktree-isolated harness accepts (no
  loops or `cd` in the caller).

## Target + blast radius

`scripts/fleet` (+ `scripts/lib/fleet_*`), `.claude/skills/fleet` (the
orchestrator skill's Procedure and Compaction sections name the verbs),
`docs/conventions/container-ops.md` if a verb needs a harness note. Nothing
in `src/`.

## Open questions / decisions log

- Decided 2026-10-03 (orchestrator verdict on design note 2,
  `reviews/ship-gate-ci.review.md` 14:56Z): held messages live in
  `<state>/fleet-queue/<win>/`, not the git-common-dir; the watch criterion
  is "re-armed only on expiry, a re-arm replays nothing" (Monitor caps an
  arm at 30 min); bare `/compact` after `/next` with a 90 s grace, and
  `compact` refuses the orchestrator's own window; `mcp-check --fix` sends
  `/mcp reconnect all`; `watch` emits on transition only and its `ci main`
  leg reports a run's conclusion, never a verdict. Verbs 1–3 and 7 shipped
  2026-10-03 (plus a `trust` dialog type and a refusal of hand-stamped
  verdict headings); 4–6 remain.
- Decided 2026-10-03: this is orchestrator-owned tooling; build it in the
  `ship-gate-ci` session (platform) as Do-next 2 behind
  `release-candidate-verdicts`, since the orchestrator does no thread work.
