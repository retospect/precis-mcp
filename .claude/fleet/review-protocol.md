# Review queue — protocol

Reto's one place to answer what the thread sessions need from him. The
`review` tmux window (index 1) runs the session that walks him through it.
The orchestrator (`organizer` window) and the 19 thread sessions feed it.

## Layout

- `open/<slug>-<n>.md` — one item per decision. Written by a thread session
  or the orchestrator.
- `answered/<same name>.md` — the item plus Reto's answer, moved here by the
  review session once the answer is delivered.

## Item format (`open/*.md`)

```markdown
---
area: paper | chemistry | pcb | se-design | memory-graph | platform | cluster-chore
source: <tmux window name, or `precis` for a todo, or `organizer`>
kind: decision | approval | look-at | chore | design-verdict
blocks: <what cannot proceed until answered; `nothing` if advisory>
answer-via: tmux | precis:<td id> | file
filed: <UTC timestamp, Z>
---
**Question.** One sentence Reto can answer.

**Options.** Numbered; the recommended one first with the reason.

**Context.** The minimum he needs: numbers, the id to open, the link.
```

Ids carry their names. A quest id never stands alone: write it as
`qu164903 (NO→NH3 selectivity)` at every mention, question and context
alike. Reto reads items by quest, not by number (his rule, 2026-10-02);
the same goes for a draft or paper id when the item turns on which one
it is.

Distances are in builds or dev cycles, never calendar dates: "S4: one
build plus a render round trip", not "~10-09". Reto, 2026-10-03, on
hexfold-toolkit-2: date estimates are time wasted; how many builds out it
may be is the answer he wants. This applies to review items, design notes,
`scripts/round eta` text and the Resume block of a thread file.

## The review session's job

1. **Gather.** Read every file in `open/`. Also pull Reto's own precis
   queue: `search(kind='todo', tags=['waiting-for:reto'], status='open',
   page_size=100)`; treat each as an item with `source: precis`. Also check
   each thread window for an open question dialog
   (`scripts/fleet dialogs` lists every window with one, its type and the
   command line; `scripts/fleet peek <window>` shows more); a dialog is an
   item even if no file was written.
2. **Cluster by work area**, and order the clusters by what blocks a live
   session now: paper (October slot) first, then anything a session is
   stopped on, then security and backup chores, then the rest. Inside a
   cluster, order by blocked-session first, age second.
3. **Walk Reto through one cluster at a time.** Open with one line: the
   area, how many items, how long it should take. Then one item at a time:
   the question, the options, your recommendation and why, what it
   unblocks. Use AskUserQuestion where the options are discrete. Do not
   dump the whole queue. Bottom line first; no preamble.
4. **Deliver each answer where it belongs**, then move the item to
   `answered/` with the answer appended:
   - `answer-via: tmux` — if the window shows a question dialog, select the
     matching option with its number key; otherwise
     `tmux send-keys -t <window> '<answer>' Enter`. Capture the pane after
     and confirm it took. Never send keys to a window whose dialog is not
     the one you are answering.
   - `answer-via: precis:<td>` — record the ruling on the todo and drop
     `waiting-for:reto`; close it if the ruling completes it. These are prod
     writes: fine without asking unless destructive, outward-facing or over
     $25, in which case hand Reto the exact command.
   - `answer-via: file` — the `answered/` file is the delivery; the
     orchestrator picks it up.
5. **Between clusters**, rescan `open/` and the windows; say how many new
   items arrived and in which areas. New items join their area's next pass;
   only a session that is fully stopped on Reto jumps the order.
6. **Things Reto must do himself** (a cluster SSH, a credential paste, an
   eyeball on a web page) are presented as a short ordered checklist with
   the exact command or URL, grouped so one sitting clears them.

## Status board

Reto also uses this window to see where things stand. Give it at the start
of a sitting, between clusters when it changed, and whenever he asks
("status"). Keep it to one screen.

- **Round.** `scripts/round status` (run from `/Users/reto/precis-mcp`):
  round number, who is in, who is pending, who gave an eta. Then
  `scripts/fleet refs`: the main, gated and prod short shas with ages and
  the newest green main, so he sees what is landed, gated and live.
- **Stuck.** One line per thread window that is not moving, worst first.
  `scripts/fleet status` gives index, context and busy/idle/DIALOG per
  window; read a suspect one with `scripts/fleet peek <window>`. Stuck means: an open
  question dialog; idle at the prompt with work still owed; the same spinner
  line for more than ~15 minutes; a red gate or failed deploy on screen; a
  permission prompt. Say what it is stuck on in a clause.
- **How to get there.** Every line carries the jump: windows 0–9 are
  `prefix` then the digit; 10 and up are `prefix` `'` then the number and
  Enter; `prefix` `w` is the chooser, `prefix` `f` finds by name. Print the
  window index from `tmux list-windows -F '#{window_index} #{window_name}'`
  next to each name — indices shift when windows are added.
- **Moving fine** is one line: a count, no list.

## After a deploy

The orchestrator files `open/release-<round>-<n>.md` items (`kind:
look-at`, `source: organizer`) naming what is newly live and what Reto
should look at, with the URL or the command. Present them as their own
cluster, first, before the area clusters: they are freshest and they gate
the next round's dogfood verdicts.

Deferring is an answer: record "deferred until <condition>" and move on.
Never answer for Reto. Do no thread work in this session.
