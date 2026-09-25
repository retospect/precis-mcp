---
status: draft
title: Restart the funding quest (qu401863) once the tick can serve a non-materials striving
prio: normal
blocked-by: quest-bodies-inquiry
---

# Restart the funding quest (qu401863) once the tick can serve a non-materials striving

`qu401863` ("A standing flow of money for open, independent research, so
the work never has to be sold") was set `STATUS:dormant` on 2026-09-25 by
Reto's decision. Parked, not renounced — the striving stands; the tick
body cannot serve it yet.

## Why it was parked

Minted 2026-09-21 active + high, the loop ran 4 ticks / 40 logbook
entries / $0.53 with **zero deeds**, cycling lit-search-finds-nothing →
agent-declines-to-propose → cost. Two causes, both now specced:

- the proposal leg only dispatches a proposal carrying an atomistic
  `structure`, so a funding proposal can never become an action and the
  deed count is structurally zero, not merely low
  (`quest-bodies-inquiry.md`);
- the force-acquire fallback appended catalysis facets to the quest's
  title, so it searched for "A standing flow of money for open,
  independent research … DFT barrier mechanism" and linked nine unrelated
  papers as servers (`quest-tick-incident-fix.md`).

Background: `gr447337`, and the decision entry on the quest's own logbook.

## Restart checklist

1. `quest-tick-incident-fix.md` has shipped and deployed — in particular
   the meta write path, without which step 3 needs hand-written prod SQL.
2. `quest-bodies-inquiry.md` has shipped and deployed.
3. Set `meta.quest_body = 'inquiry'` on `qu401863` via the new write path.
4. Confirm on a dry-run (`precis quest tick 401863 --dry-run`) that the
   assembled prompt carries none of "measured barriers", "awaiting a sim",
   "candidate materials to simulate", or the `structure` JSON example, and
   that the fallback query contains no catalysis facet.
5. `tag(kind='quest', id=401863, add=['STATUS:active'])`.
6. Watch the first three ticks. The failure to look for is the inquiry
   body's open question — a tick logging `milestone` entries for having
   thought about something. If deeds climb without an artifact existing,
   park it again and fix the deed definition first.

## Note on what is NOT blocked

The two serving todos are human work and stay open regardless:
`td401864` (independent-research funding pipeline — sweep calls, ingest
each as `kind='cfp'`, spin fits into proposal projects per
`precis-proposal-help`) and `td401865` (university-routed grant path —
which programs, which named academic partner could be PI).

Deleting this file is the ship: when the quest is active again and has
logged one honest deed, fold nothing anywhere — `git log` and the quest's
own logbook carry it.
