---
status: ready
title: Bootstrap roadmap quest — a `roadmap` tick body that grows a capability/pathway/rung DAG from measured gaps
prio: high
model: fable
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
2.1 [fi…]").

The model cannot emit `milestone` — and that half is **already enforced**,
not new work: `quest/tick.py::_sanitize_model_entry` clamps any
model-authored `result`/`milestone`/`cost` entry down to `observation`
against its `_MODEL_ALLOWED_ENTRY_TYPES` allowlist, on the standard
`_stage_apply` path this body reuses (found by the `quest-bodies` readiness
review, 2026-09-27). So this item builds only the *positive* half — the
code-stamped deed on a ledger improvement — and inherits the clamp. A
roadmap tick that stamps nothing therefore reports a deed count of zero
honestly, with no prompt rule needed.

**Stall / halt:** a tick that changes no gap count and no ledger value is a
dry tick; reuse the existing `consecutive_dry_rests` escalation
(`quest_tick.py::_bump_dry_rest` region) — 3 dry ticks → cool + alert.
Minting a rung does NOT reset the dry counter (the `quest-bodies.md` perverse
incentive: only a ledger improvement counts as engagement). Rung mint is
near-dup matched against existing rungs on the same capability: call the
token-Jaccard **measure** directly (`dossier.py::_find_near_dup_node`) over
sibling rung titles, as a gate before the `todo` is minted. Do **not** route
this through `add_attempt` — that writes into a single quest's own free-text
attempt ledger and gates nothing about node creation; borrowing it as a
side-channel would leave dossier residue per rejected mint.

**Rung minting boundary:** rungs mint as `STATUS:open` + `waiting-for:reto`,
**`llm_tier` unset** — a human flips them into the doable rotation (same
ruling as `quest-bodies.md` §pursuit item 3: the mint is the spend). A new
pathway quest mints `STATUS:dormant`; a human activates it.

*Amended 2026-09-29 (Reto: option B).* This said `STATUS:proposed`, which
`parse_strict` refuses — `STATUS` is a closed vocabulary
(`store/types.py::_CLOSED_VOCAB`) holding `open/doing/blocked/done/won't-do`
plus the finding-chase values, and `proposed` is not among them, so the
bridge role's mint would have failed at write time. Adding it was the obvious
repair and is the wrong one: every existing query that treats
`open|doing|blocked|done` as a partition of the todo space would skip the new
value, turning "not picked up by the rotation" into "invisible" — and a
proposal nobody sees is a proposal nobody sanctions.

**`llm_tier` unset is the actual lock**, not the status: the dispatch worker
selects on `meta ? 'llm_tier'` (`workers/dispatch.py` — `AND NOT (… .meta ?
'llm_tier')` in both the parent-eligibility and candidate queries), so a rung
without it is already not a candidate. `STATUS:open` therefore keeps rungs
visible in ordinary todo views (intended — they need human eyes) while the
missing `llm_tier` keeps them un-worked, and `waiting-for:reto` says why.
Same shape as `graph-gardener.md`'s proposals, so the two agree.

Consequence for AC3: assert `llm_tier` **absent** and the `waiting-for:reto`
tag present — do not assert a `proposed` status.

**Knowledge that outlives a quest** goes into a skill per pathway (agents
write `precis-bootstrap-<pathway>` via `put(kind='skill')`), not into prompt
growth. Dossiers stay per quest and are rewritten.

**Terminal vs intermediate rungs (the benign gate).** The root's fifth rubric
clause — the end product is safe to handle and comes apart into harmless
pieces after use — applies to what the chain *delivers*, not to every rung
along the way; Reto's ruling is that intermediates get it as a preference,
not a requirement, because the chain is run once. That split is **derived
from the graph, not annotated**: a rung whose `produces` appears in no other
rung's `consumes` is terminal, and the bridge role treats the benign
capability (qu454479) as a required unmet capability when it mints or
completes such a rung, advisory otherwise. An explicit
`meta.rung.benign = "required"` overrides the derivation upward (never
downward) for a rung whose product leaves the lab despite being consumed.

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
- No change to the materials or weave bodies. `inquiry` **has shipped**
  (`weave_tick.py::QUEST_BODY_INQUIRY`, wired into `quest_tick.py::
  _quest_body`/`_phase_tick`) and owns the prompt-stripping and
  frontier-says-none behaviour that `roadmap` reuses. Note the frontier check
  is a literal `== QUEST_BODY_INQUIRY` equality, not a catch-all for
  non-materials bodies, so AC7 requires a real code change rather than
  passing for free.
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
   lands `STATUS:open` + `waiting-for:reto` and **carries no `llm_tier` key
   at all** (assert absence — that is the rotation lock, not the status),
   serves both the pathway and the capability, and a second identical bridge
   tick mints **no** twin (dedup).
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
8. Materials-body identity: `tests/test_quest_tick.py::
   test_materials_default_still_carries_the_tokens` and
   `::test_default_unset_resolves_to_materials` still pass unchanged — a
   fourth arm must not perturb the default. (These are the live tests; the
   `quest-bodies.md` "AC4" this originally cited never existed — the
   numbering came from `quest-bodies-inquiry.md`, deleted on ship.)
9. Docs: `precis-roadmap-help` exists and `search(kind='skill', q='rung
   consumes produces capability')` returns it. Not "returns it first" —
   ranking is embedding-search against a live corpus and would make the
   assertion flaky for reasons unrelated to this body.

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

## Residuals after the 2026-09-29 build (items 1–6 built, skills not)

Items 1, 2, 3, 4, 5, 6 are BUILT (commits `2d6aab0a`, `1a4cb076`, `4adfe448`,
`97f93c6b`). Item 7 (skills) is not. Remaining known gaps:

1. **`meta.rung.benign = "required"` cannot be stored.** The "Terminal vs
   intermediate rungs" section specifies it as an upward-only override, but
   `handlers/_todo_guards.py::_RUNG_ALLOWED_KEYS` is a closed set
   `{pathway, consumes, produces}`, so the write is refused. `rung_is_terminal()`
   honours the key if present and the bridge honours a model-emitted one at
   mint time, but **no door admits it** — the override is dead until `benign`
   joins that allowlist with a value check. One-line change plus a test; not
   done during the build because the guard file was off-limits to the agent
   that found it.
2. **"Lowest unmet capability" was never defined.** Implemented as "the
   capability consumed by the most other rungs' `consumes`" (a chain-depth
   proxy), ties by `serves` link order. Reasonable, but it is the builder's
   reading, not a ruling — revisit if the root picks surprising capabilities.
3. **Supply-absent rows.** With `demand` set and no supply, the ledger says
   `unmet` and `quest_gaps()` emits `unmet-capability`, which the role table
   routes to *bridge* — but bridging with no supply number is meaningless.
   Role selection therefore splits unmet rows: cited supply present → bridge,
   none → supply. The role table above should be read with that in mind.
4. **Deed baseline on the first tick.** "Improved since the previous ledger
   chunk" is literal: the pinned chunk carries `meta.signature` and the next
   tick diffs it. On the very first tick the baseline is that tick's own
   starting ledger, so pre-existing supplies seed silently while a supply the
   first tick itself writes does get stamped.
5. **Duplicated rung-status query.** `roadmap_tick` re-derives rung statuses
   with its own SQL because `roadmap_ledger._rungs_for` / `_DONE_STATUSES` are
   private. Exporting `rungs_for(store, capabilities)` from the ledger module
   removes the duplication.
6. Not built, by scope: the capped *framing* chunk from the §Dossier-shape
   ruling, and the web hub ledger panel (spec already says it can trail).

## Open questions / decisions log

- **Where does `supply` live when a rung is done?** Decided 2026-09-27,
  **revised twice, final 2026-09-28: `meta.supply` stays.** The 09-27
  revision routed supply through the `measures` table so nothing copied
  numbers between nodes. That table does not exist, `measures-substrate.md`
  is itself `blocked-by: term-taxonomy`, and neither is built — so the
  revision made this item the third in a chain for a dependency it does not
  need. Nothing in the motivation requires a general measures substrate; the
  ask is "a deed is a capability's cited best-supply number improving,"
  which `meta.supply` with non-empty `evidence` satisfies standalone. The
  09-27 revision was also never propagated — Design, In-scope 2 and ACs 2–5
  still read `meta.supply` — which made AC4's supply clause vacuous: with
  `supply` dropped from `_META_ALLOWED_KEYS`, the write is refused for being
  an unknown key, so the test would pass without the evidence check ever
  being built. Reading supply from `measures` is a **v2 widening**, to be
  specced after `term-taxonomy` → `measures-substrate` land; it is not
  tested here. The staleness cost is accepted for v1: a retracted finding
  needs `meta.supply` edited as well as its hub, and catching that drift is
  `graph-gardener.md`'s problem, not this body's.
- **Dossier shape (Reto, 2026-09-27):** the roadmap body does NOT rewrite
  a narrative. It writes a capped *framing* chunk (a few sentences,
  handles only, no bare numbers, length cap enforced in code) plus the
  regenerated capability-ledger chunk. Reports are views (ledger · series
  · contradictions · coverage) and a cached render stamped with the graph
  revision; a written document goes through the weave body. The
  narrative growth-ratchet gate does not apply to this body.
- **Axes vocabulary.** Decided: reuse `rubric_objectives` `{key, sense}` on
  the capability quest; no new registry. `unit` is a free string in v1.
- **`model: fable` in the frontmatter is deliberate** — Reto's call, and the
  Agent tool takes `model='fable'` directly. It is the only such value in
  `docs/backlog/` and `TEMPLATE.md` lists only sonnet/opus/haiku, so a
  readiness pass reads it as a typo. It is not one; do not "fix" it to
  `opus`. Nothing in `scripts/` routes this field automatically — dispatch
  is by hand either way.
- **Model tiers. Decided 2026-09-28: the role picks the tier, in code, with
  no knob.** `quest-redispatch-tier.md` is the wrong anchor — it re-scores a
  simulation's `barrier_fidelity` ladder (`quest/compute.py::
  redispatch_candidates`), an unrelated sense of "tier". The real per-tick
  mechanism (`quest/loop.py::_loop_params`, `meta.loop.tier`) is fixed when
  the coordinator job is minted and held for the job's whole life, so it
  cannot vary by role within one quest. Shape: role selection is a pure
  graph read, so `_phase_roadmap_tick` calls `roadmap_role(store, quest_id)`
  **first**, builds `DispatchClient(tier=…)` at that role's tier (mirroring
  the weave arm's own construction at `quest_tick.py`'s `source=
  "quest_weave"` client), then calls `roadmap_tick` with it. `meta.loop.tier`
  is ignored for this body. No `meta.roadmap_tiers` — unrequested scope.
- **Does a pathway quest itself tick? Decided 2026-09-28: no — root and
  capability quests only.** The earlier "yes, same body" answer contradicted
  the gap→role table directly above it. All three roles key off a quest's own
  `rubric_objectives`/`demand`/`supply`, and by the Design table only
  *capability* quests carry `rubric_objectives`. A pathway's gap set is
  therefore always the four generic ones (`thin-support`, `no-literature`,
  `low-mastery`, `open-hypothesis`), none of which the role table maps — so a
  ticking pathway is a guaranteed dry tick, and three dry ticks trips the
  cool+alert escalation. Every active pathway would false-alarm on a cadence.
  Pathways stay dormant and carry the marker only so their rungs render;
  `mark_roadmap_quest` does not make a quest tickable on its own.
- **Narrowed 2026-09-29 to ROOT-ONLY ticking.** "Root and capability" has the
  same defect one step down: the root carries no `rubric_objectives` either
  (only capabilities do), so a root whose role selection reads only its *own*
  meta is dry on every tick — and if root and capability both tick, both can
  mint a rung for the same gap. So: **exactly one loop, on the root.** The
  root's role selection scans the capability quests that `serve` it, picks the
  highest-priority gap across them, and acts on *that* capability — which is
  what "the lowest unmet capability first" in the Design section already
  implies. Capability quests are data, not loops: they hold
  `rubric_objectives`/`demand`/`supply` and are written by the root's tick,
  never ticked themselves. One dry counter, one ledger, one writer.
  Consequence for In-scope 3: `quest_gaps()` emits `unmet-capability` for a
  roadmap **root** by scanning its served capabilities, with `Gap.handle` set
  to the capability's handle — not by reading the ticked quest's own meta.
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
