# Review session — protocol

Reto's one place to see where things stand and to answer what the thread
sessions need from him. The `review` tmux window (index 1) runs the session
that walks him through it. The orchestrator (`organizer` window) and the 19
thread sessions feed it.

There is no review-queue directory. A question for Reto is an `ask-user`
todo with `meta.ask`, filed through the precis MCP from any machine; the
list is `search(kind='todo', view='asks')` and the web `/asks` page. How to
file and answer one: `get(kind='skill', id='precis-work-help')`, section
"How do I ask the owner a question?".

## Writing a question

Rules for the todo text and `meta.ask.context`:

- An item that tells Reto something is ready to sign or approve (a nanopub,
  a hub) must cite a passing gate dry run of the exact wording and payload
  he will submit: `get(kind='finding', id=…, view='mint-preflight',
  args={'payload': …})` or `precis nanopub check`, with the PASS line and
  when it ran. A claim edited after that run needs a new run. A sign item
  without it is not ready (fi263188, 2026-10-03: "ready to sign" was filed
  on a wording the claim-sentence gate refused).
- Ids carry their names. A quest id never stands alone: write it as
  `qu164903 (NO→NH3 selectivity)` at every mention, question and context
  alike. Reto reads items by quest, not by number (his rule, 2026-10-02);
  the same goes for a draft or paper id when the item turns on which one
  it is.
- Distances are in builds or dev cycles, never calendar dates: "S4: one
  build plus a render round trip", not "~10-09". Reto, 2026-10-03, on
  hexfold-toolkit-2: date estimates are time wasted; how many builds out it
  may be is the answer he wants. This applies to questions, design notes,
  `scripts/round eta` text and the Resume block of a thread file.

## The review session's job

1. **Gather.** `search(kind='todo', view='asks')`: showstoppers, then
   decisions, then discussion, grouped by quest and thread. Also check each
   thread window for an open question dialog (`scripts/fleet dialogs` lists
   every window with one, its type and the command line; `scripts/fleet peek
   <window>` shows more); a dialog is an item even if no todo was filed.
2. **Cluster by work area**, and order the clusters by what blocks a live
   session now: paper (October slot) first, then anything a session is
   stopped on, then security and backup chores, then the rest. Inside a
   cluster, order by blocked-session first, age second.
3. **Walk Reto through one cluster at a time.** Open with one line: the
   area, how many items, how long it should take. Then one item at a time:
   the question, the options, your recommendation and why, what it
   unblocks. Use AskUserQuestion where the options are discrete. Do not
   dump the whole queue. Bottom line first; no preamble.
4. **Deliver each answer where it belongs.**
   - An `ask-user` todo — comment `answer: …` on it, then
     `tag(kind='todo', id=N, add=['STATUS:done'], remove=['ask-user'])`
     (the `/asks` form does both). These are prod writes: fine without
     asking unless destructive, outward-facing or over $25, in which case
     hand Reto the exact command.
   - A question dialog in a window — select the matching option with its
     number key; otherwise `tmux send-keys -t <window> '<answer>' Enter`.
     Capture the pane after and confirm it took. Never send keys to a window
     whose dialog is not the one you are answering.
5. **Between clusters**, re-run the asks view and check the windows; say how
   many new items arrived and in which areas. New items join their area's
   next pass; only a session that is fully stopped on Reto jumps the order.
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

The orchestrator files an `ask-user` todo per look-at (`meta.ask` kind
`approval`, blocks `nothing`) naming what is newly live and what Reto
should look at, with the URL or the command. Present them as their own
cluster, first, before the area clusters: they are freshest and they gate
the next round's dogfood verdicts.

Deferring is an answer: record "deferred until <condition>" and move on.
Never answer for Reto. Do no thread work in this session.
