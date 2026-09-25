---
status: draft
title: Quest bodies — an inquiry body, so a striving that isn't a materials search can tick
prio: normal
blocked-by: quest-tick-incident-fix
---

# Quest bodies — an inquiry body, so a striving that isn't a materials search can tick

## Motivation / why

Request (Reto, 2026-09-23): "fix the shape, and make a proposal to make
better quests of many types." First draft proposed a new `meta.flavour`
axis with a three-value inference table; a Fable review (2026-09-24)
killed that design on evidence and this is the rework. The narrower
incident fixes moved to `quest-tick-incident-fix.md`, which ships first.

The quest layer is sold as the aim layer over *any* striving —
`precis-quest-help` opens with "heal the environment". The tick is a
materials search. A striving that isn't one can reason and read, but it
can never produce anything the tick recognises as an action, because the
proposal leg only dispatches a proposal carrying an atomistic `structure`
(`quest/tick.py`); anything else lands as a `hypothesis` logbook entry,
is re-seen next tick, and is proposed again. Deeds are `milestone`
entries and only `quest/graduate.py` stamps one, so a non-materials
quest's deed count is **structurally** zero, not merely low.

Two corrections to the first draft's diagnosis, both verified:

- **It is not the *catalyst* prompt that leaks.** The "relentless
  catalysis researcher" creed (`tick.py::_explorers_creed`) and the
  `add_atom_site`/`add_adsorbate` menu live inside
  `tick.py::_reaction_context`, which returns `""` without
  `meta.reaction_config`. What an unmarked quest actually inherits is the
  generic **materials** template: "measured barriers … awaiting a sim",
  "candidate materials to simulate — each an atomistic `structure`", and
  the `structure` JSON example, all unconditional in `_PROMPT_TEMPLATE`.
  The defect is real; its name was wrong, and so were the tokens the
  first draft's acceptance criterion grepped for.
- **A body selector already exists.** `meta.quest_body` routes
  `quest_tick.py::_phase_tick` to an entirely different tick for
  `"weave"` (`quest/weave_tick.py::QUEST_BODY_META_KEY`,
  `quest_tick.py::_quest_body`). A new `meta.flavour` would have been a
  fifth switch (beside `compute_lane`, `reaction_config`,
  `rubric_objectives`, `quest_body`) overlapping `quest_body` in meaning
  exactly.

## In scope

**One new value on the existing axis: `meta.quest_body = "inquiry"`.**

| `meta.quest_body` | body |
|---|---|
| unset | `materials` — today's behaviour, unchanged |
| `"weave"` | the weave tick (exists) |
| `"inquiry"` | new: read, reason, synthesise; no proposals, no frontier |

**Never infer the body from other meta.** This is the load-bearing
constraint and the reason the first draft was rejected. `qu202467` is a
live materials campaign — `tick_count: 291`, `results_seen: 336`,
`ticks_since_experiment: 0` — and carries **no** `reaction_config`, **no**
`compute_lane`, **no** `rubric_objectives`. Any rule of the form "no
`reaction_config` ⇒ not a materials quest" silently demotes it, costing
it its proposal menu, its frontier and its compute. Default unset ⇒
`materials`, explicit opt-in only, no backfill.

`reaction_config` stays what it is: an optional catalysis overlay *inside*
the materials body, not a body of its own. `compute_lane` stays an
orthogonal knob.

The `inquiry` body supplies three things the materials body currently
inlines:

1. **No proposal menu.** Drop the `structure` schema and the "candidate
   materials to simulate" block from the assembled prompt. This is
   exactly §2 of `quest-compute-lane-runs-on-a-literature-only-quest.md`,
   absorbed here.
2. **No frontier.** `view='frontier'` on an `inquiry` quest says the body
   has no frontier, rather than rendering today's "(none converged yet)"
   — which reads as "nothing has converged" when the truth is "nothing
   ever will".
3. **A defined deed.** Open question below; without one, an inquiry tick
   logs whatever the model chooses to call a milestone, which is how a
   loop reports progress while making none.

## Explicitly NOT in scope

- **`pursuit` is deferred** — see the stub below. The first draft had the
  tick autonomously minting `cfp` refs and proposal-project todos; that
  is not ready to spec.
- No new column, no migration. `meta.quest_body` exists.
- No change to the materials or weave bodies. The materials body must be
  today's code paths, not a reimplementation of them.
- Not a plugin system. Three named bodies in one dispatch; a fourth is a
  code change, deliberately.
- Does not touch loop arming (`PRECIS_QUEST_LOOP_ENABLED`) — that is
  `quest-loop-activation.md` — nor
  `quest-rubric-unproducible-objectives-warning.md`.

## Deferred: the `pursuit` body

An operational striving (find funding, get this published, find a fab)
wants a body whose proposals are external artifacts. It is **not**
specifiable yet. Four prerequisites, each its own problem:

1. **No discovery source.** Every body keeps lit-search, but lit-search
   finds *papers* (S2 + paper facets). Nothing in precis finds funding
   calls. Without one, a pursuit tick can only propose calls the model
   recalls from training — which is precisely the "three recalled funding
   leads, self-flagged unverified" entry that `gr447337` already flags as
   the failure mode.
2. **`cfp` has no put.** `handlers/cfp.py` sets `supports_put=False`; a
   cfp is acquired by ingesting a PDF (`precis add --as cfp`, or the
   `inbox/cfp/` watch folder). "Dispatch is a `put`" was simply false.
3. **Minting is the spend.** A proposal-project todo carries
   `meta.llm_tier='opus'` and is then ticked autonomously by the
   `dispatch` worker. "A human still decides what is submitted" is a
   vacuous boundary — precis submits nothing, ever; the decision that
   costs money is the *mint*, and the first draft handed it to the LLM.
   If it ships: mint as `STATUS:proposed` + `waiting-for:reto` with
   `llm_tier` **unset**, and a human flips it.
4. **No dedup, and a perverse incentive.** A dispatch resets the tick's
   stall counter, so if minting is the only route to a deed, the loop is
   rewarded for minting one project per tick — the same call re-minted
   every slice.

## Acceptance criteria

1. An `inquiry` quest's assembled prompt (`precis quest tick <id>
   --dry-run`) contains none of: "measured barriers", "awaiting a sim",
   "candidate materials to simulate", the `structure` JSON example. Name
   these tokens, not the `reaction_config`-gated ones — those never
   reached an unmarked quest.
2. An `inquiry` tick emits no proposal menu and mints no `relax` /
   `autocatpath` job (the verify line from
   `quest-compute-lane-runs-on-a-literature-only-quest.md`).
3. `view='frontier'` on an `inquiry` quest says the body has no frontier,
   distinct from today's "(none converged yet)".
4. **Materials-body identity.** Not "byte-identical prompt on qu164903" —
   untestable, since the prompt embeds a live dossier, a live logbook tail
   and `quest_momentum`'s `now()`-relative 14-day window. Instead:
   (a) snapshot the assembled prompt for the existing `reaction_config`
   fixture in `tests/test_quest_tick.py` on main, assert equality after
   the refactor, delete the snapshot in the same ship; and (b) assert the
   materials body's creed / proposal / ranking hooks *are* the existing
   `_explorers_creed` / `_reaction_context` / frontier callables.
5. A quest with empty meta resolves to `materials`. `qu164903`,
   `qu202467` and `qu202469` all resolve to today's behaviour with no
   backfill. This criterion is the first draft's inverted: it had empty
   meta resolving to `inquiry`, which breaks `qu202467`.

## Target + blast radius

- `src/precis/workers/job_types/quest_tick.py` — `_phase_tick`,
  `_quest_body`; the dispatch gains a third arm.
- `src/precis/quest/tick.py` — `build_tick_prompt`, `_PROMPT_TEMPLATE`
  (the unconditional materials blocks), the dry-tick commit ladder.
- `src/precis/quest/frontier.py` — `pareto_split` / the frontier render
  behind a body check.
- `src/precis/handlers/quest.py` — `_render_health_and_gaps`, the `view=`
  set, the "(none converged yet)" render.
- `src/precis/quest/weave_tick.py` — `QUEST_BODY_META_KEY` is the shared
  constant; `inquiry` joins it.
- Runtime docs: `precis-quest-help` (body values + the `view=` table),
  `precis-quest-writing-help` (choosing a body when minting).

## Open questions / decisions log

- **What is a deed for `inquiry`?** `materials` has a graduated
  candidate; `pursuit` would have a submission. An inquiry quest has
  nothing comparable, and "the model logged a milestone" is not a deed.
  Candidates: a finding hub minted and survived adversarial dispute; a
  dossier attempt-ledger node moving to `ruled-out` on evidence. Not
  blocking body dispatch, but it is the next thing that will look wrong.
- **Does `inquiry` force `compute_lane='off'`?** Proposed yes, as a
  consequence of the body rather than a second switch to set.
- **Backfill.** Which live quests want an explicit `inquiry`?
  `qu202469` declares literature-only in its dossier prose with nothing
  set (named in `quest-compute-lane-runs-on-a-literature-only-quest.md`
  §1). `qu401863` is the other. Both need
  `quest-tick-incident-fix.md`'s write path first — hence `blocked-by`.
- **Does the old item get deleted?** `quest-compute-lane-runs-on-a-
  literature-only-quest.md` §1 is absorbed by the write path in the
  incident fix and §2 by item 1 here; delete it when the second of the
  two ships.
