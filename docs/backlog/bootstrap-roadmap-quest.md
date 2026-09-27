---
status: draft
title: Bootstrap roadmap quest — a `roadmap` tick body that grows a capability/pathway/rung DAG from measured gaps
prio: high
model: fable
blocked-by: quest-bodies
---

# Bootstrap roadmap quest — a `roadmap` tick body that grows a capability/pathway/rung DAG from measured gaps

## Motivation / why

Reto, 2026-09-27: the linear-motor / photonic-arm strivings are one bet
inside a larger one — **the Bootstrap**: a chain of assemblers, each built by
the one before it, in liquid phase at room temperature, ending at
diamondoid / graphenic parts (the se/hexfold target set in
`nanomachine-framework-goal`). Candidate bets: DNA scaffolding + photocontrolled
walkers + λ-orthogonal photochemistry (the Barner-Kowollik / Heckel / Liu / Na
Liu line), light-driven mechanical arms (qu330435 + the qu347422 cluster),
protein machinery, scanning-probe assembly in liquid. "We'll add more as we
think of it." The ask: a structure that does a top-down (what a target part
demands) AND bottom-up (what the literature can deliver today) analysis and
yields a chain of currently-doable steps, each building the next — and that
stays **flexible and growing** without anyone maintaining a master plan.

Why it cannot be built on today's tick: both running nanomachine quests were
captured by the materials body. qu330435 sits at 385 logbook entries, tote
$148, **0 deeds**, and its dossier is entirely about S-adatom slab
constructions failing the structure preflight; qu347422 is on tick 4 of the
same `slab needs 'element' and 'size'` stall. The materials body can only
recognise an atomistic `structure` as an action (`quest-bodies.md`), so a
roadmap-shaped striving degenerates into geometry fights. A Bootstrap quest
minted active with the default body drifts the same way within a week.

The `quest-bodies.md` `inquiry` body strips the proposal menu but leaves
"what is a deed for `inquiry`?" open. This item answers it for the roadmap
shape: **a deed is a capability's cited best-supply number improving**, stamped
by code from the graph, never by the model calling something a milestone.

## Design (the shape, in existing kinds)

Three node types, no new kind:

| node | kind | meaning |
|---|---|---|
| **pathway** | quest, `quest_body="roadmap"` | one bet (DNA scaffold, light mechanical, protein, probe-in-liquid, photochem alphabet). Add a bet = one `put` + `serves` link; retire = `STATUS:dormant`, its rungs stay under their capability. |
| **capability** | quest, `quest_body="roadmap"`, `meta.rubric_objectives` = the measurable axes | cross-cutting spine (positional accuracy, cycle time, force, error/step, addressable wavelengths, survives-drying). Every pathway serves several. |
| **rung** | todo with `meta.rung` | completable step; `consumes` capability values, `produces` capability values with `fi` evidence; `serves` one pathway AND one capability; `blocked-by` chains between rungs ARE the roadmap. |

Bootstrap is the root roadmap quest, serves qu161906 (PRIO flows down; the
light-world striving is why the light-driven bet exists).

**Meta shapes** (all JSON, all validated on write):

- capability quest: `meta.rubric_objectives = [{key, sense, unit}]` (exists,
  allowlisted; `unit` is a new optional field). New allowlisted keys:
  `meta.demand = {key: {value, source, reason}}` (source = `se:<slug>` /
  `td<id>` / `qu<id>` — where the number came from) and
  `meta.supply = {key: {value, evidence: ["fi<id>", …]}}` (best value the
  literature delivers today; `evidence` non-empty or the write is refused).
- rung todo: `meta.rung = {pathway: "qu<id>", consumes: [{capability:
  "qu<id>", key, value}], produces: [{capability: "qu<id>", key, value,
  evidence: ["fi<id>", …]}]}`. A `produces` entry with empty `evidence` is
  refused — **no number, no rung**.

**Derived (never stored) reads:**

- *best supply* for (capability, key) = the better of `meta.supply[key]` and
  every **done** rung's `produces` for that key (sense from
  `rubric_objectives`).
- gap **`unmet-capability`** (new gap type in `quest_gaps()`): a
  `rubric_objectives` key with a `demand` entry whose best supply does not
  meet it, and no open/active rung whose `produces` would.
- **capability ledger**: one row per capability quest under a roadmap root —
  key · demanded · best supply (with its evidence handle) · closing rung
  (`td`, status) · state ∈ {unmet, partial, met, dead-end}. Rendered by
  `view='tree'` and pinned into the root's dossier as ONE regenerated chunk
  (`meta.pinned='capability-ledger'`, text = the table; rewritten from the
  graph each tick, so it cannot go stale; it is a materialised render, not a
  per-node store — capabilities are already addressable as quests).

**The `roadmap` tick — one role per tick, chosen by gap type:**

| gap seen | role | one action |
|---|---|---|
| a capability has a `rubric_objectives` key with no `demand` | **demand** (top-down) | read the se/hexfold part(s) that `serve` the root (or the part named in the capability's statement), compute what the part requires on that axis, write `meta.demand[key]` with the calculation in a `decision` logbook entry. Sonnet; reasoning only. |
| `demand` present, `supply` absent, or `no-literature` | **supply** (bottom-up) | lit-search + read-for-question on that capability, mint finding hubs with quantified quotes, write `meta.supply[key]` (evidence = the hubs). Sonnet; literature lane only. |
| `unmet-capability` | **bridge** | compare demand vs best supply. Outcomes: (a) supply meets demand → mint a rung with `produces` = the cited number; (b) shortfall → mint a rung whose deliverable is the shortfall, or — if no existing pathway plausibly covers it — a **new pathway quest** serving the root; (c) nothing plausible → `dead-end` logbook entry carrying the number, ledger state `dead-end`. Opus. |

Priority among gaps: the lowest **unmet** capability first (it blocks the chain
above it), then thin/no-literature. One action per tick, then end. No
proposal menu, no `structure` schema, no frontier (`view='frontier'` says the
body has none — shared with `inquiry`), `compute_lane` irrelevant (the body
never dispatches relax/autocatpath).

**Deed (answers the `inquiry` open question):** at tick end the body
recomputes the ledger; any (capability, key) whose best supply improved since
the previous ledger chunk gets a code-stamped `milestone` entry on the
capability AND the root ("supply on `positional_accuracy_nm` improved 6.0 →
2.1 [fi…]"). The model cannot emit `milestone`.

**Stall / halt:** a tick that changes no gap count and no ledger value is a
dry tick; reuse the existing `consecutive_dry_rests` escalation
(`quest_tick.py::_bump_dry_rest` region) — 3 dry ticks → cool + alert.
Minting a rung does NOT reset the dry counter (the `quest-bodies.md` perverse
incentive: only a ledger improvement counts as engagement). Rung mint is
near-dup matched against existing rungs on the same capability (reuse the
attempt ledger's token-Jaccard dedup, `dossier.py::add_attempt`).

**Rung minting boundary:** rungs mint as `STATUS:proposed` +
`waiting-for:reto`, `llm_tier` unset — a human flips them into the doable
rotation (same ruling as `quest-bodies.md` §pursuit item 3: the mint is the
spend). A new pathway quest mints `STATUS:dormant`; a human activates it.

**Knowledge that outlives a quest** goes into a skill per pathway (agents
write `precis-bootstrap-<pathway>` via `put(kind='skill')`), not into prompt
growth. Dossiers stay per quest and are rewritten.

## In scope

1. `meta.quest_body = "roadmap"` — fourth arm in
   `quest_tick.py::_phase_tick` (mirror the weave arm at the
   `_quest_body(...) == QUEST_BODY_WEAVE` check); new module
   `src/precis/quest/roadmap_tick.py` with `roadmap_tick(store, client,
   quest_id, *, dry_run)` returning the weave-shaped dict the coordinator
   reads (`ok`, `applied`, `note`, plus `role`, `gap`, `ledger_delta`);
   `mark_roadmap_quest(store, quest_id)`.
2. Meta validation: `demand` / `supply` join
   `handlers/quest.py::_META_ALLOWED_KEYS` with shape checks; `rung` gets a
   `check_rung_meta` guard in `handlers/_todo_guards.py` (put path; and add
   `rung` to `TAG_META_ALLOWED_KEYS` so a rung's produces can be updated on
   completion without a re-put).
3. `unmet-capability` gap in `quest/gaps.py::quest_gaps()`; the tree render
   picks it up unchanged.
4. Capability ledger: `quest/roadmap_ledger.py` (compute from graph; render
   markdown; regenerate the pinned chunk — pattern of
   `dossier.py::_ensure_frontier_tree_chunk_for_ref`); shown in `view='tree'`
   for roadmap quests and in `view='dossier'` via the pinned chunk.
5. Code-stamped deed on ledger improvement.
6. CLI parity: `precis quest tick <id> --dry-run` prints the chosen role +
   gap + the assembled prompt; `precis quest set <id> quest_body roadmap`.
7. Runtime docs: `precis-quest-help` (body table + ledger view),
   `precis-quest-writing-help` (when to mint a roadmap quest; the
   pathway/capability/rung vocabulary), new `precis-roadmap-help` (rung meta
   shape, the three roles, the no-number-no-rung rule).

## Explicitly NOT in scope

- **No prod data in this item's ship.** Minting Bootstrap, the pathway and
  capability quests, re-parenting qu330435, dormanting the motor cluster,
  and ingesting the paper list are one-off prod writes Reto runs by hand
  (the call list is in "Open questions" below for reference; it moves to a
  runbook or dies with this file).
- The `simulations` evidence route (`quest-bodies.md` §evidence parity) —
  the supply role is literature-only in v1; simulation as a supply source is
  the natural v2 once `sandbox_run` is unblocked (gr329258).
- No demand *calculator*. v1 demand role is the model reading an se part's
  measures and writing a number with its reasoning; a deterministic
  se → demand mapping is a later item.
- No change to the materials or weave bodies; `inquiry` (blocked-by) ships
  first and owns the prompt-stripping and frontier-says-none behaviour that
  `roadmap` reuses.
- Not a plugin system. Four named bodies in one dispatch.
- No migration: everything is `meta` + `links` + chunks.

## Acceptance criteria

1. `precis quest tick <roadmap-id> --dry-run` on a fixture root with one
   capability lacking `demand` prints `role: demand` and a prompt containing
   the served se part's measures and none of: "measured barriers", "awaiting
   a sim", "candidate materials to simulate", the `structure` JSON example.
2. Same fixture with `demand` set and no `supply`: `role: supply`; the tick
   emits `searches` and mints no `relax`/`autocatpath`/`sandbox_run` job.
3. Fixture with `demand` and a worse `supply`: `quest_gaps()` yields exactly
   one `unmet-capability` gap; the tick runs `role: bridge`; a minted rung
   lands `STATUS:proposed` + `waiting-for:reto`, `llm_tier` unset, serves
   both the pathway and the capability, and a second identical bridge tick
   mints **no** twin (dedup).
4. `put(kind='todo', meta={'rung': {... produces: [{..., evidence: []}]}})`
   is refused with a message naming the rule; `edit(kind='quest',
   meta={'supply': {'k': {'value': 1}}})` (no evidence) is refused.
5. Marking a rung `done` whose `produces` beats the stored `supply` makes the
   next tick (a) render the ledger row with the rung as best supply, (b)
   stamp exactly one `milestone` on the capability and one on the root, by
   `system`, (c) reset the dry counter. A tick that changes nothing bumps
   `consecutive_dry_rests`; three → the existing cool-down + alert.
6. `view='tree'` on the root shows the capability ledger table; the root's
   dossier has exactly one `meta.pinned='capability-ledger'` chunk after N
   ticks (regenerated, not appended).
7. `view='frontier'` on a roadmap quest says the body has no frontier.
8. Materials-body identity: the `quest-bodies.md` AC4 snapshot test still
   passes; empty meta still resolves to `materials`.
9. Docs: `precis-roadmap-help` exists and `search(kind='skill', q='rung
   consumes produces capability')` returns it top.

## Target + blast radius

- `src/precis/workers/job_types/quest_tick.py` — `_phase_tick` (4th arm),
  dry-rest counter reuse.
- `src/precis/quest/roadmap_tick.py` (new), `src/precis/quest/roadmap_ledger.py`
  (new), `src/precis/quest/gaps.py` (`unmet-capability`),
  `src/precis/quest/dossier.py` (pinned-chunk helper generalised),
  `src/precis/quest/weave_tick.py` (`QUEST_BODY_META_KEY` shared constant).
- `src/precis/handlers/quest.py` — `_META_ALLOWED_KEYS`, `_render_tree`,
  frontier render; `src/precis/handlers/_todo_guards.py` — `check_rung_meta`,
  `TAG_META_ALLOWED_KEYS`.
- `src/precis/cli/quest.py` — `--dry-run` role print.
- Tests: `tests/test_quest_gaps.py`, `tests/test_quest_tick.py`,
  `tests/test_dossier_hygiene.py`, new `tests/test_quest_roadmap.py`.
- Skills: `precis-quest-help`, `precis-quest-writing-help`,
  `precis-roadmap-help` (new).
- Web: `/refs/quest/<id>` hub — ledger panel for roadmap quests (can trail).

## Open questions / decisions log

- **Where does `supply` live when a rung is done?** Decided: derived at read
  time (max over `meta.supply` and done rungs' `produces`); nothing copies
  numbers between nodes, so a retracted finding only needs its hub edited.
- **Axes vocabulary.** Decided: reuse `rubric_objectives` `{key, sense}` on
  the capability quest; no new registry. `unit` is a free string in v1.
- **Model tiers.** demand/supply Sonnet, bridge Opus, via the existing
  per-tick tier plumbing (`quest-redispatch-tier.md` if it lands first;
  else a `meta.roadmap_tiers` knob — decide at `ready`).
- **Does a pathway quest itself tick?** v1: yes, same body — its gaps are
  its own thin-support/no-literature, and its bridge role can only mint
  rungs (not sub-pathways). Root-only ticking would be simpler; decide at
  `ready` after the first dry-run.
- **Prod data — DONE 2026-09-27 (all dormant, `quest_body=roadmap`,
  `compute_lane=off`; activate nothing until the body ships):** root
  `qu453863` (serves qu161906). Pathways: qu453865 DNA-scaffold toolhead ·
  qu453866 protein/enzymatic · qu453867 scanning-probe in liquid · qu453868
  λ-orthogonal covalent alphabet · qu330435 light-driven mechanical
  (re-parented; its `serves qu161906` edge dropped; qu347422 + qu347481-4
  stay under it, all dormant). Capabilities (with `rubric_objectives`):
  qu453869 positional accuracy · qu453870 bond-making toolhead · qu453874
  readout/error correction · qu453875 feedstock delivery · qu453876
  replication · qu453877 drying without collapse · qu453878 addressable
  channels. Papers: 14 stubs linked (8 → DNA-scaffold incl. Seeman pa347475,
  6 → alphabet incl. Menzel = 10.1002/anie.201901275, Leigh walker →
  qu330435). `put(kind='paper')` takes no `link=`; link by `pa<id>` after.
