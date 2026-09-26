---
status: draft
prio: normal
---

# Quest bodies

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Quest bodies — an inquiry body, so a striving that isn't a materials search can tick

_Grouped 2026-09-26; was `quest-bodies-inquiry`, status draft, prio normal, blocked-by quest-tick-incident-fix._

### Motivation / why

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

### In scope

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

### Explicitly NOT in scope

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

### Deferred: the `pursuit` body

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

### Acceptance criteria

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

### Target + blast radius

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

### Open questions / decisions log

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

## Restart the funding quest (qu401863) once the tick can serve a non-materials striving

_Grouped 2026-09-26; was `quest-401863-restart`, status draft, prio normal, blocked-by quest-bodies-inquiry._

`qu401863` ("A standing flow of money for open, independent research, so
the work never has to be sold") was set `STATUS:dormant` on 2026-09-25 by
Reto's decision. Parked, not renounced — the striving stands; the tick
body cannot serve it yet.

### Why it was parked

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

### Restart checklist

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

### Note on what is NOT blocked

The two serving todos are human work and stay open regardless:
`td401864` (independent-research funding pipeline — sweep calls, ingest
each as `kind='cfp'`, spin fits into proposal projects per
`precis-proposal-help`) and `td401865` (university-routed grant path —
which programs, which named academic partner could be PI).

Deleting this file is the ship: when the quest is active again and has
logged one honest deed, fold nothing anywhere — `git log` and the quest's
own logbook carry it.

## Quest evidence parity — simulation as a first-class evidence route

_Grouped 2026-09-26; was `quest-simulation-evidence-parity`, status draft._

Reto, 2026-09-08: *"it is ok to be not found in literature if we can show
something is working"* — and, choosing the scope: **"full evidence parity once
we learn; we have for example ml potential that provides feedback in the other
quest."**

### The defect

`src/precis/quest/tick.py` gives the tick model exactly **one** way to acquire
evidence — `searches`, i.e. the literature:

> *"Progress means new external evidence, not more restating. … When the answer
> lies in the literature you don't yet hold … emit `searches` to go get it
> instead of hypothesising in a vacuum."*

There is no other affordance in the prompt. So "not in the literature" reads as
a **dead end by construction**, and the only sanctioned move is to search again.

This is observable, not theoretical. Quest `qu330435` (photonic assembly arm)
entry 7 proposed a molecular charge-coupled-device analogue and logged it as
*"an analogy drawn here, not a literature finding. No molecular CCD has been
verified to exist"* — then queued another search. The idea was sound; the prompt
had no vocabulary for "so simulate it and see".

### What already exists

* **`sandbox_run`** (ADR-0048) is a registered job type with a params schema,
  executed by `claude_docker`. It is the substrate. Nothing in a quest tick can
  request it.
* **The catalysis/frontier lane already does this properly** — an ML potential
  computes structure relaxations that feed back into the quest's frontier, and
  `precis.quest.graduate` encodes the epistemic boundary in its own docstring:
  *"A simulation is not the world"*, graduating a candidate that crosses a bar
  to a `needs-experiment` gap for a human/lab.

**Model the design on that lane. Do not invent a parallel mechanism.** It is a
working instance of computed evidence feeding a quest, with the
simulation-vs-world boundary already drawn. Read it first.

### Consequence of the gap

Because the tick could not ask, `qu330435` entry 10's Monte Carlo was written
out-of-band to `~/precis-experiments/photonic-arm/mc_protocol.py` on one
operator's laptop: not a ref, not citable by handle, not re-runnable, invisible
to the dialectic, and lost if that machine goes away.

### The epistemics to encode (the model already got this right unprompted)

Entry 10 framed itself as *"deliberately NOT a yield prediction — p_bit for a
photochemically clocked molecular register is unmeasured, so the sim sweeps it
and returns the REQUIREMENT instead, turning the missing number into a spec on
the chemistry rather than a blocker."*

That is the behaviour worth making explicit: **a simulation over an unmeasured
parameter returns a SPEC, not a result.** The prompt should ask for that
framing, not merely grant permission to simulate. It converts a blocker into a
falsifiable requirement on the physical system.

### Scope: full parity

1. **Prompt** — rewrite the evidence paragraph so computed evidence counts as
   progress and absence from the literature is not a terminal state; require the
   spec-not-prediction framing.
2. **Affordance** — a `simulations` output field beside `searches`, dispatched
   as `sandbox_run`, whose result lands as a `result` logbook entry plus a
   stored artifact addressable by handle.
3. **Dialectic parity** — a simulation may `support`/`counter` a hypothesis with
   its own handle class and an explicitly **weaker epistemic tier** than a
   trusted measurement, extending `graduate.py`'s boundary into the dossier so a
   reader can never mistake a simulated support for a measured one.

### Blockers / notes

* `sandbox_run` slice 1 is live but **blocked on `gr329258`** (vault
  `CLAUDE_CODE_OAUTH_TOKEN` 401), so step 2 is buildable but not
  smoke-testable end-to-end until that is re-minted.
* Step 3 is the largest change to a load-bearing research prompt in the repo —
  `tick.py` is ~3k lines and drives every quest. Stage it behind 1 and 2.
