---
status: in-progress
title: Measures substrate — generalise material_values into `measures` (any subject ref, literal kept, reference state, anchored evidence edges) so quantbind, quests and the mesh share one number record
prio: high
model: opus
pillar: memory-graph
---

# Measures substrate — generalise `material_values` into `measures`

## Motivation / why

Two trees need a persisted, sourced number at once: the quantbind
extraction reader (`corpus-quantitative-extraction.md`, `norr-her-meta.md`,
tree `paper-extraction`) produces materialized results and has nowhere to
land them but JSON; the knowledge mesh (`knowledge-mesh.md`) and the
Bootstrap roadmap (`bootstrap-roadmap-quest.md`) need to read "best value
on axis K over everything serving quest Q". The materials frontier
assembles its numbers at read time from `struct_runs` + `structure.meta`
and a prod audit found `material_values` at 0 rows
(`quest-data-table-and-formula-discovery.md`) — that figure is dead as of
a 2026-09-28 recount (8 rows; see the decisions log and Target + blast
radius for the full measured set).

`material_values` (migration 0092) already holds value_num/low/high/text/bool,
`conditions`, `maturity`, `method`, `source_ref_id` + `source_chunk`, a
reserved `input_unit`, and a registry `material_properties` with
canonical unit, `value_type`, `core`/`proposed` status and
`higher_is_better`. The extraction item's "Prior art in-tree" already says
reuse it. Its gaps, per that item and the paper-extraction session's
review of the mesh draft (2026-09-27): keyed on a *material* ref only; no
exact literal; no reference state / convention; per-condition evidence
anchors dropped; one text chunk handle instead of anchored evidence
edges; no epistemic state beyond `method`; no unit conversion.

This item is the contract both trees build against.

## AMENDMENT 2026-09-28 — ordering inverted, and `measurands` is gone

Two changes decided by Reto with `paper-extraction-15`. They supersede
every mention of a `measurands` table below; that text is stale until
this item is rewritten at build time, and a coder must read this section
first.

**1. `term-taxonomy` ships first.** This item is no longer "first and
alone" — it is `blocked-by: term-taxonomy`, which turns the three
parallel registries (`material_properties` 24, `component_specs` 30 +
`component_categories` 13, `rxn_properties` 10) into `taxon` refs. So:

- **Do not create a `measurands` table.** `measures` gains
  `measurand_ref_id bigint NOT NULL REFERENCES refs (ref_id)` pointing at
  a `kind='taxon'` node (handler-enforced, as `material_ref_id` already
  is — `refs` has no per-kind FK).
- Everything this item hung on `measurands` moves or dies:
  `required_conditions` becomes a start-node `contract.required_keys`
  entry; `aliases` is node meta; `reference_states` and `convert` stay
  here as measure-side columns, since they are per-value conventions,
  not per-term facts.
- The `material_properties` compatibility view now projects **over taxon
  refs**, not over a renamed table. The `material_values` view and its
  `INSTEAD OF INSERT` trigger are unaffected.
- The fold-component question is dissolved: all three registries become
  nodes in `term-taxonomy`'s seed, so there is no "which registry
  migrates" choice left here. Delete that decision thread on rewrite.

**2. Four trust gaps, all measure-side.** The existing `tier` /
`source_attribution` / anchored-edge machinery covers whether *the
paper's* number is sound. It does not cover whether *we read it right*.

- **`extraction_status`** — `unverified | anchor_matched | anchor_mismatch
  | human_checked`, **computed on write, never asserted**: does `literal`
  actually occur in the anchored span? Every existing guard checks that
  an anchor *exists*, none checks that it *matches*, and right-paper
  wrong-column is the dominant LLM extraction failure. A mismatch flags
  the row, never rejects it.
- **`trusted boolean` becomes derived, not settable.** It is a verdict
  with no reason and no author, which contradicts the ruling (agreed for
  taxon nodes) that truth lives on annotation `finding` rows through the
  existing verdict/dispute path. Keep the column as a fast denormalised
  read; compute it from findings.
- **`derived_from bigint[]`** — a `tier='derived'` row must record its
  input measure ids so effective trust can be computed as the **minimum
  over inputs**. Today a value derived from one measured and one asserted
  input is indistinguishable from a measured one.
- **Structured writer provenance.** `set_by` is free text. The corpus
  finding that claim errors cluster by source (fix rates 6%–62% across
  paper clusters) means "every measure written by model M in pass P" has
  to be a query; free text will not cluster.

**3. A new `experiment` ref kind, which this item owns.** `term-taxonomy`
holds classes only; an individual run is an instance and belongs here.
`measures` gains `experiment_ref_id`, and the shared context of a run
(temperature, loading, …) stops being copied into each row's `conditions`
JSONB and becomes **measures rows with `direction='input'` against the same
experiment** — a set temperature and a measured one have identical shape
and differ only by direction, so `conditions` as a separate
representation is redundant.

`direction` is `input | output | covariate` and is a **different axis
from `role`**, which stays the condition-role enum `context |
preparation | model`. A condition row carries both: it is an input *and*
it is context-vs-preparation-vs-model. Overloading `role` with
input/output would break the `required_conditions` rule, which reads
`role='context'`, on its first run. This makes "what else came out of this run" a query, and gives
inputs the same `literal`/`tier`/provenance treatment as outputs, which
matters because a stated reaction temperature is itself a sourced claim.
Rules: an experiment belongs to **exactly one paper** (the one that ran
it), `instance-of` a taxon experiment-type node, and evidence spanning
papers (B uses A's method) is reached through the *method node's* own
anchors, not by the experiment owning two papers — otherwise no paper
owns its conditions when two disagree.

**4. `measures` is append-only.** `chunks` is already append-only for
body rows because in-place UPDATE strands the downstream cascade; the
same argument applies to a reviewed number. Never UPDATE — supersede with
a new row plus a `supersedes` pointer, with a partial index on the live
rows. Post-review tampering then cannot happen rather than having to be
detected, and history is free. On review, additionally hash the
load-bearing fields (literal, value, unit, measurand, subject,
experiment, anchor) into the review record: under append-only that hash
should never mismatch, which makes it an invariant alarm rather than a
routine check.

## Pilot build on qu202467 (Reto 2026-10-03, knowledge-mesh-12)

Reto chose to build this item now, with qu202467 (NO from exhaust →
fertilizer N) as its first consumer. The plan: the measures table,
hand-minted domain taxa, the quest's 140 findings extracted into
reviewed measures, and fisheye on the quest. This section is the
build-time rewrite the amendment above asked for. Where it differs from
the older text below, this section wins.

**The input.** The quest's findings, read from prod 2026-10-03:
- **Anchors:** 130 of the 140 have a `corroborates` link from a paper
  with `src_chunk_id` set, 132 anchors in all, every chunk live, median
  677 characters.
- **Numbers:** 17 claims hold no number. Counted over claim sentences,
  by pattern:

  | measurand | claims |
  |---|---|
  | Faradaic efficiency | 52 |
  | yield rate | 27 |
  | potential vs RHE | 23 |
  | partial current density | 16 |
  | selectivity | 16 |
  | DFT energies (eV) | 14 |
  | duration | 11 |
  | pressure | 9 |
  | concentration | 9 |

  None of the 79 prod taxa covers any of these.

**Build A: the migration, on a branch to the orchestrator.** Everything
decided earlier holds:
- the rename and compatibility views;
- the `component_spec_values` fold-in (option ii);
- one canonical unit per measurand, values stored normalised;
- the four trust gaps;
- append-only rows.

What this build decides:
- **The measurand comes from the 0174 seed.** `measurand_ref_id` is
  backfilled through the taxon's `meta.legacy_source`: every
  `material_properties`, `component_specs` and `rxn_properties` row
  became a taxon there. A legacy row whose property maps to no taxon
  stops the migration and names the property.
- **The migration is one transaction,** so a stop leaves prod
  untouched.
- **Conditions are `direction='input'` rows joined to their output by
  `run_key text NOT NULL`** (orchestrator fix, 2026-10-03).
  - An output and exactly its own inputs share one `run_key`. Grouping
    by finding is not enough: "FE 95% at −0.5 V and 61% at −0.9 V" is
    two outputs and two potentials in one claim, and it must become two
    runs.
  - Extraction mints one key per claim × condition set; the
    compatibility views set it from the legacy row id.
  - `subject_group` stays as the paper-local group label (the finding
    handle in the pilot).
  - `conditions jsonb` stays only for the compatibility views.
- **`insert_measure` writes a run.** It takes one output and its input
  rows together in one `tx()`, and the `required_keys` check runs over
  that run.
- **A missing required key flags the row; it is never refused.**
  `required_keys` writes the miss into `meta.escalation`, and
  `best_measure` excludes flagged rows. Only 23 of the 140 claims state
  a potential in the sentence, so refusing would drop most of the 52
  Faradaic-efficiency rows.
- **The `experiment` kind is deferred.** The column
  `experiment_ref_id bigint NULL` ships now, and the kind fills it from
  `run_key` one to one when it ships. A kind that owns one paper's runs
  is a separate design and isn't needed to store these numbers.
- **The evidence edge gets a new relation, `quantifies`.** It runs
  from the paper (with `src_chunk_id` set) to the measurand taxon,
  with edge `meta = {anchor_scheme, span}`.
  - Why the taxon as the target: "this chunk of this paper quantifies
    this measurand" is true of every row. A taxon's fisheye then lists
    the papers that measure it, which is the census.
  - It is inserted outside `add_link`, as in-scope 2 says.
  - The insert bypasses `add_link`, so `quantifies` must be registered
    wherever the relation vocabulary is checked: the `Relation`
    literal, the relations table if there is one, the walker's
    allow-lists and the `precis-relations` skill table.
  - Input rows carry their own `primary_link_id` too, since a stated
    temperature is a sourced claim. AC 2's refusal covers
    `tier='measured'` inputs.
- **The load-bearing fields are frozen by a trigger.**
  - **Frozen:** `literal`, `value_*`, `reported_unit`,
    `measurand_ref_id`, `subject*`, `run_key`, `experiment_ref_id`,
    `direction`, `tier`, `reference`, `normalization*`,
    `primary_link_id`, `actor` and `model`. An UPDATE to any of them
    is refused, naming the rule.
  - **Updatable:**
    - `superseded_by` and `superseded_at`;
    - `trusted`, computed from findings later;
    - `extraction_status`, but only to `human_checked`.

    These are annotations, not content.
  - "Live" means `superseded_by IS NULL`, which a partial index covers.
    Both compatibility views filter on it.
  - **No INSTEAD OF UPDATE trigger.** No store op updates
    `material_values` or `component_spec_values`; a grep of
    `src/precis/store` is empty.
- **The ledger learns a fourth target, `reviews.target_kind = 'measure'`.**
  `precis_target_sha('measure', id)` hashes the frozen fields only.
  - **Why only those:** an annotation update never stales a review,
    and the frozen fields never change, so a stale measure review is
    an invariant alarm, which is what amendment §4 asks for.
  - **Order:** 0185 has to land first. 0185 and 0187 go to the
    orchestrator as two branches, in that order.
- **Who wrote each row is two columns, `actor` and `model`,** the same
  pair as `reviews`, in place of free-text `set_by`. A legacy `set_by`
  maps into `actor`; a NULL one becomes `actor='legacy'`.
- **`trusted` stays NULL** until findings-derived trust ships.
- **A taxon with live measures refuses a change to `canonical_unit`.**
  This check goes in the taxon handler, with one test. It means a
  later dedup against `list.v1.yaml` cannot re-base stored values
  silently.
- **Units are converted with pint.** It is already a dependency, and
  `taxonomy/normalise.py::resolve_dimension` parses units. The
  converter:
  - normalises Unicode superscripts and `·`;
  - refuses a unit that has no dimension match with the measurand,
    naming both units;
  - keeps the reported unit and literal as provenance;
  - records `reference` (RHE, SHE, …) beside the value, never folded
    into the unit;
  - converts mass rates to amount rates through the product's molar
    mass, which is computed from the product's formula, a required key
    on yield rate. Most yield rates in this domain are mass rates: in
    the quest's text, 17 are `mg` or `µg h⁻¹ cm⁻²` / `mg_cat⁻¹` and 8 are
    `mol h⁻¹ cm⁻²`. A product with no formula flags the row;
  - moves a subscript basis label on a denominator into
    `normalization`, such as `mg_cat⁻¹` (per catalyst mass) or
    `mg_Fe⁻¹` (per metal mass). The column also keeps geometric area
    and electrochemically active surface area (ECSA) apart: both are
    mol s⁻¹ m⁻², and they must never compare (AC 3's 3.7 vs 9.25
    pair).
- **Store ops:**
  - `insert_measure`, which:
    - takes the link and the row in one `tx()`;
    - computes `extraction_status`: does the literal occur in the
      span's text?;
    - enforces the guards from in-scope 3;
    - enforces `required_keys` on the measurand;
  - `measures_for(subject_ref_id)`.
- **Done when:**
  - ACs 1–4 pass;
  - a reviewed measure round-trips through the ledger and shows
    `current`;
  - an UPDATE to a frozen field is refused, while one to `trusted`
    passes and does not stale the review;
  - two AC 3 fixtures pass:
    - an output with two potentials in one finding lands as two runs;
    - a `µg h⁻¹ cm⁻²` yield-rate literal converts to the canonical unit
      through the product's molar mass.
- **Migration number:** 0187, assigned by the orchestrator on
  2026-10-03.

**Build B (qland, after A deploys):**
- `best_measure` and `measures_census`;
- `precis-measure-help`;
- ACs 5–8.

**The pilot's taxa.** About ten, minted by hand in prod under
`measurand` via `put` plus a `specialises` link. These are ordinary prod
writes.
- **Measurands:**

  | taxon | canonical unit |
  |---|---|
  | Faradaic efficiency | % |
  | product yield rate per geometric area | mol s⁻¹ m⁻² |
  | product yield rate per catalyst mass | mol s⁻¹ kg⁻¹ |
  | partial current density | A m⁻² |
  | applied potential | V; the reference is a column |
  | product selectivity | % |
  | reactant conversion | % |
  | adsorption / reaction energy | eV |
  | stability duration | s |

- **Product-specific measurands carry required context.** Faradaic
  efficiency, yield rate, selectivity and partial current density each
  take `contract.required_keys = ['product']`, plus `potential` for
  Faradaic efficiency and partial current density. That is
  the spec's own Faradaic-efficiency example. Per-area and per-mass
  yields are separate taxa because they differ in dimension, so no
  single canonical unit covers both. That is what `normalization`
  could not settle.
- **Inputs:** NO partial pressure, electrolyte concentration and
  temperature are measurands too, written as `direction='input'` rows.
- **The full run will reach these taxa.** When the taxonomy run
  produces `list.v1.yaml` it covers this domain (the NORR/HER survey
  hubs), and taxon dedup merges its nodes with these.

**Build C: extraction (qland, after A deploys).** One pass over the
findings:
- **Extraction:** a mid-tier model turns each claim and its anchored
  chunk into rows: literal, unit, measurand, subject label (e.g. "Cu
  NWA"), `subject_group` = the finding handle, the input rows, and the
  span as raw offsets in the chunk.
- **Subject:** `subject_ref_id` is the paper, which reaches the quest
  through `serves`.
- **Tier:** `measured` only for `own_work` with a matched anchor.
- **Review:** a bigger model checks each row against the chunk text and
  writes a `reviews` row with its verdict, model and version.
- **What Reto gets:**
  - the anchor-mismatch rows;
  - the rejections;
  - the rows the two models disagree on;
  - a `best_measure` table per route family.

**Build D: fisheye on the quest (qland, independent).** The ladder on
`kind='quest'` (fisheye-everywhere AC 2). In flight 2026-10-03.

## In scope

**1. Migration (forward-only).** Rename `material_values` → `measures`.
There is **no `measurands` table** (amendment above, 2026-09-28):
`measures.measurand_ref_id` points at a `kind='taxon'` ref, and
`material_properties` is retired by `term-taxonomy.md`'s seed. Keep
`material_values` as an **updatable view** for one release so
`handlers/material.py` / `component.py` are untouched by this item. Every
later mention of `measurands` in this section reads as "the taxon ref".

A single-table rename-projection view over `measures` is auto-updatable in
Postgres, but that covers only the columns the legacy writer already names.
`MaterialMixin.material_value_insert` (`src/precis/store/_material_ops.py::MaterialMixin.material_value_insert`)
does an explicit-column `INSERT INTO material_values (...)` that never names
`literal`; once `measures.literal` is `NOT NULL` that insert fails on every
call, which is exactly the case plain auto-updatability doesn't reach. So
`literal NOT NULL` stays (literal-first is the point) and the
`material_values` view gets an **`INSTEAD OF INSERT` trigger** that
synthesises `literal` from whichever of `value_num` / `value_text` /
`value_bool` the legacy caller supplied, rendered as the printed form —
load-bearing, not belt-and-braces. The migration backfills `literal` on
existing rows by the same rule — load-bearing on this item's own first
run, not theoretical: a 2026-09-28 read-only prod query found
`material_values` at 8 rows, not 0 (see the pre-migration row-count check
under Target + blast radius, below, which stays required since this count
moves).

Column changes on `measures`:

| change | why |
|---|---|
| `material_ref_id` → `subject_ref_id` (any ref: material, paper, finding hub, structure, se design — **not component in v1**, see "Explicitly NOT in scope") | rows hang off **papers by default**; a hub is minted only when a result is promoted to a claim (extraction session's cardinality ruling — 66 results/paper must not become 66 hubs) |
| `literal text NOT NULL` | the exact reported string; parsed `value_num`/`value_low`/`value_high`/`value_text` become the derived, nullable reading (`9.6 ± 1.7` → literal kept, num 9.6, `value_err` 1.7; `<1` → `value_form='upper_bound'`; `550–575` → interval; `Cordierite` → `value_form='categorical'`, num NULL) |
| `value_form text CHECK (point|approximate_point|upper_bound|lower_bound|interval|categorical|boolean|not_established)` + `value_err double` | |
| `reference text` | reference state / convention (RHE, SHE, Ag/AgCl, ΔE vs ΔG, per-atom vs per-mole) — an **input to the number**, not a unit alias (SHE↔RHE differs by 0.059·pH). Census today: 150 survey hubs state a potential with no reference, 185 with one |
| `tier text CHECK (measured|computed|derived|asserted) NULL` | evidence-parity ladder; `asserted` = stated, unattributed; **NULL = could not establish** — a distinct state, never defaulted into `asserted` |
| `trusted boolean NULL` | generalises `barrier_trusted`; NULL = unassessed |
| `conditions jsonb` element shape | `{name, role: context|preparation|model, value, unit, reference?, basis_chunk_id?, basis_anchor?, applicability_anchor?}` — the per-condition anchors stay, or the escalation `applicability_not_shown_for_this_result` has nothing to point at. Three roles, domain-neutral: `context` = conditions of the observation (potential, electrolyte, temperature; or country, year, survey wave), `preparation` = how the subject/sample was made or constructed, `model` = an analytical assumption (DFT functional, estimator, CI type). Same three roles quantbind already extracts under chemistry names — `operating` renamed to `context`, `preparation`/`model` unchanged 1:1 — so the extraction reader needs no change. `knowledge-mesh.md`'s numeric-conflict rule matches on "operating conditions"; read that as `context` conditions once this lands (not edited here) |
| `source_chunk` (text handle) **deprecated**, kept for the view | evidence moves to `links` rows: `measures.primary_link_id` FK links NOT NULL when `tier='measured'`; further edges allowed (pilot averages ~2 spans/result). Edge `meta = {anchor_scheme, span}` — the scheme belongs to the anchor, so it lives on the edge, not the row. `span` holds all four quantbind forms: sentence `"6132.4"`, sentence range `"6132.4-6"`, numeric atom `"6132#3"`, raw offsets `[chunk,start,end]` |
| `subject text` + `subject_group text` | **paper-local object identity** (quantbind `s`/`x`: "PdCoP-2", "Cu(111) with O-vacancy"; group/context id). `subject_ref_id`=paper alone loses which sample the number belongs to, and per-catalyst figures need it. A ref is the right subject only once the object is promoted to a structure/material ref; until then the label is the identity |
| `normalization text` + `normalization_status text CHECK (explicit|inferred|unresolved)` + anchor in `meta.normalization_anchor` | **normalization basis** (per geometric cm² · per mg catalyst · per ECSA). Not the same axis as `reference`. `3.7 mg h⁻¹ cm⁻²` and `9.25 mg h⁻¹ mgcat⁻¹` from one sentence must never compare |
| `source_attribution text CHECK (own_work|cited_work|not_established)` | a paper restating another paper's number is `cited_work`; **`cited_work` never gets `tier='measured'`** (write guard) — it is `asserted` on this subject and a pointer to chase the primary |
| `measurand_status text CHECK (explicit|interpreted|ambiguous)` | `best_measure` **excludes `ambiguous`** |
| `meta jsonb` | `source_caveat` (authors declare their own result non-reproducible — a source-stated reliability caveat, NOT a dispute), `limitations` (per-result `lim`), escalation severity, extraction run id |

`measurands` gains: `reference_states jsonb` (declared allowed references
per measurand), `convert jsonb` (convert / never-convert rules, e.g.
SHE→RHE needs pH; ΔE↔ΔG never), `aliases jsonb` (measurand strings a
reader may emit), `required_conditions text[]` (the `context`-role
condition names that must be present on a row of this measurand — a
write, or a `best_measure` read, whose row lacks a declared required
condition raises the escalation, whatever the domain; e.g. Faradaic
efficiency requires `potential` + `feed`, an unemployment rate requires
`year` + `population_definition`). This replaces the keyword-matching "is
an operating temperature visible" guard in
`corpus-quantitative-extraction.md` item 2 with a registry rule — that
item's chemistry patch becomes a `required_conditions` row instead of
code. Key identity for aliasing and for comparison is `(measurand, reference,
convention, normalization)`, not `(key, unit)`.

`dimension` (already on `material_properties`, a descriptive quantity-kind
label) needs values beyond SI once non-chemistry measurands register:
dimensionless ratio, count, and currency-with-a-price-base-year cover
most of them — a registry data question, not a migration. The existing
`reference` and `normalization` columns already cover the non-chemistry
analogues (ILO-vs-national definition, pre-tax vs post-tax, per-capita)
with no schema change needed.

**If the fold-component decision below resolves to (ii)** (open, Reto's
call — see decisions log): the `measurands` step of this migration is a
merge of two curated registries, not a create-then-populate step —
`material_properties` (24 rows) and `component_specs` (30 rows) merge
into one table, and `component_categories` (13 rows) becomes the initial
`subject_class` vocabulary. The migration must check
`material_properties.prop_id` against `component_specs.spec_id` for slug
collisions before assigning both into one `measurand_id` namespace (AC9).

**2. Store ops.** `insert_measure`, `measures_for(subject_ref_id)`,
`best_measure(key, *, serving=<quest handle>, sense, reference=None)`
(walks `serves` any depth; `trusted IS NOT FALSE`; refuses to compare
rows with differing `reference` unless a `convert` rule applies),
`measures_census(key)` (count by tier/reference — the norr-her-meta
step-1 read).

`insert_measure`'s evidence edge does **not** go through `add_link`
(`src/precis/store/_links_ops.py::add_link`): that dedup key —
`(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)` — excludes
`meta`, where `span` lives, so two measures anchored to the same chunk with
different spans (this item's own ~2-spans/result case, and the AC3
table-recipe row) would collide onto one link row and both inherit the
first one's span. `insert_measure` gets its own non-deduping insert path;
`add_link`'s key is not widened, since every other kind shares it. `span`
stays on the edge rather than moving to the measure row because
`anchor_scheme` + `span` belong to the anchor, per the paper-extraction
session's constraint. Ordering is simple: insert the link and the measure
row in the same `tx()`, then set `measures.primary_link_id` from the
returned link id — no deferred constraint needed, since `links` never
references `measures` back.

**3. Write guards.** `tier='measured'` without a `primary_link_id` whose
link row has `src_chunk_id` set is refused, naming the rule;
`source_attribution='cited_work'` with `tier='measured'` is refused. A
`measurands` row of status `core` cannot be minted by a put (migration
only), `proposed` can — same rule as today.

**4. One canonical unit per measurand; values stored normalised**
(Reto 2026-10-02, knowledge-mesh-7: "unify the units and just store the
value" — a barrier is always eV, never meV). Every measurand taxon node,
`core` or `proposed`, carries exactly one canonical unit, fixed when the
node is minted (the taxonomy list's `canonical_unit`). A put converts the
reported value into it via `convert`; `value_num`/`low`/`high` are always
in the canonical unit. The reported literal and unit stay as provenance
(`literal`, `reported_unit`), never as the value. A unit that cannot be
converted to the measurand's dimension is refused, naming both units
(this replaces the earlier "store `reported_unit` with `value_num` NULL"
rule for `proposed` measurands).

**5. Docs.** `precis-material-help` (values now live in `measures`;
verbs unchanged), new `precis-measure-help` (row shape, tiers, the
literal-first rule, reference states), `docs/reference/schema.md` regen.

## Explicitly NOT in scope

- The extraction reader, anchor scheme, recipes, escalation ranking,
  where the reader pass runs (`corpus-quantitative-extraction.md`; the
  paper-extraction session's decision: a queued job via a todo, never an
  in-process direct call).
- The walk verb, numeric-conflict disputes, the `quest_mesh` service,
  frontier adapter, capability supply (`knowledge-mesh.md`, blocked-by
  this).
- A measurand ontology beyond the registry + aliases; cross-paper
  identity resolution.
- Hub promotion policy (when a paper-owned row becomes a claim hub) —
  the findings/nanopub path owns it.
- Changing `handlers/material.py` / `component.py` call sites — the
  views keep them green; migrating them to `measures` directly is a
  follow-on.
- Folding `component_spec_values` / `component_specs` (migration
  `0093_component_kind.sql`) into `measures` — **no longer flatly out of
  scope as of 2026-09-28.** A prod recount found `component_spec_values`
  at 68 rows against `material_values`' 8 — about 89% of all sourced
  values in the system live in the table this item was going to leave
  outside `measures`. The decisions log below now recommends folding it
  in (option (ii)), with shipping v1 without component (option (i)) kept
  as the fallback. **Ruled 2026-09-30 (Reto): option (ii), fold it in.**
  `component_spec_values` is a second copy of the shape this item retires
  and moves into `measures` in the same migration, with a compat view of
  its own so `handlers/component.py`'s live call site stays green for one
  release, the same treatment as `material_values`.

## Acceptance criteria

1. After the migration all `material_values` rows — 8 as measured
   2026-09-28, recheck before running per Target + blast radius since this
   count moves — are readable through both `measures` and the
   `material_values` view with identical values, `literal` populated by
   the backfill rule; `tests/test_material*` and `tests/test_component*`
   pass unchanged.
2. Inserting `tier='measured'` without an anchored primary edge is
   refused naming the rule; with an edge whose meta carries
   `anchor_scheme` + `span` it lands.
3. Round-trips of the following hand-built fixture rows, given inline here
   (in the `paper-extraction-pilot/format-v4.json` *shape* — that file is a
   field-schema reference, not the source of these values, and the real
   round outputs carrying quoted source text are gitignored by design):
   `9.6 ± 1.7`,
   `<1`, `550–575`, a categorical row, a row with NULL tier, a row with a
   `preparation` temperature condition, **two results from one paper with
   the same measurand and different `subject` labels, a `cited_work` row
   (lands `asserted`, refused as `measured`), a `3.7 mg h⁻¹ cm⁻²` vs
   `9.25 mg h⁻¹ mgcat⁻¹` pair that `best_measure` never compares, an
   `ambiguous` measurand row that `best_measure` skips, a table-recipe
   row with `reported_unit` NULL and the measurand unit known, and one
   edge per span form (sentence, range, numeric atom, raw offsets)** —
   each lands with `literal` intact and the parsed columns as the table
   above states; the `preparation` temperature does not satisfy a required
   `context` condition check.
4. Two measures anchored to the **same chunk** with different spans
   produce two distinct link rows, each carrying its own `span`; neither
   inherits the other's (exercises `insert_measure`'s non-`add_link`
   insert path, not `add_link`'s dedup key).
5. `best_measure('U_L', serving='qu…', reference='RHE')` ignores a row
   stated vs SHE with no pH, and includes it when the condition set
   carries pH and the `convert` rule applies.
6. A non-chemistry round-trip: a measurand with `required_conditions`
   `{year, population_definition}` accepts a row carrying both `context`
   conditions and escalates one missing `population_definition`; two rows
   differing only in `normalization` (per-capita vs absolute) are never
   compared by `best_measure`.
7. `measures_census('U_L')` reproduces the 150-no-reference / 185-with-
   reference split on a fixture built from the survey hubs.
8. `precis quest table <quest>` (from `quest-data-table…` stage 1) can
   be built as a plain select over `measures` — demonstrated by a test
   that selects `computed` rows for a fixture structure.
9. If the fold-component decision (decisions log) resolves to (ii):
   merging `material_properties` and `component_specs` into `measurands`
   produces no `prop_id`/`spec_id` slug collision across the two source
   registries, or names each collision explicitly and how it's resolved
   — the merged registry's primary-key namespace is unique before either
   side reads through it.

## Target + blast radius

- `src/precis/migrations/<next>_measures.sql` (rename + views + columns +
  the `material_values` `INSTEAD OF INSERT` trigger that synthesises
  `literal` for the legacy write path, plus the `literal` backfill on
  existing rows).
- `src/precis/store/_measures_ops.py` (new); `handlers/material.py`,
  `handlers/component.py` untouched (views).
- Skills: `precis-material-help`, `precis-measure-help` (new).
- `docs/reference/schema.md` regen via `scripts/bump`.
- Prod, before running the migration: reconfirm the live row count with
  a read-only `scripts/prod-psql "SELECT count(*) FROM material_values"`
  — measured 2026-09-28 at 8 rows (`material_properties` 24: 17 core / 7
  proposed; `component_spec_values` 68; `component_specs` 30: 28 core / 2
  proposed; `component_categories` 13 — see the decisions log for what
  the last two mean for this item), superseding the 2026-08-17 audit's
  "0 rows", which is dead. This count moves, so recheck rather than reuse
  this figure — alongside the existing check of the prod migration ledger
  for a number collision (`migration-collision-renumber-recipe`). The
  `literal` backfill rule above is load-bearing against a real 8 rows, not
  a theoretical safeguard against a hypothetical nonzero count.

## Open questions / decisions log

- **[decided 2026-10-02, Reto knowledge-mesh-7]** One canonical unit per
  measurand, values stored normalised to it, the reported value and unit
  kept only as provenance. Applies to `proposed` measurands too (in-scope
  4 amended). The `component_spec_values` fold-in inherits it: datasheet
  ratings land in the measurand's unit (V, A, °C), never as printed.

- **Rename vs new table.** Decided: rename + compatibility views. A
  third sourcing grammar beside `material` and `component` was the thing
  both trees agreed not to ship.
- **Subject default = paper.** Decided (extraction session): rows hang
  off the paper ref; a hub owns a row only after promotion. `best_measure`
  therefore walks `serves` from papers as well as hubs.
- **Who ships.** Decided 2026-09-27: **shoestring-quest (this tree)**.
  paper-extraction's round writes JSON; its port trigger is the first
  cited figure, which comes after this lands. Its v4 review (subject
  identity, normalization, source attribution, measurand status, enum
  additions, span forms) is folded into the table above; everything
  else in the fixture list round-trips as drafted.
- **blocker: `literal text NOT NULL` breaks the compat-view write path.**
  `MaterialMixin.material_value_insert` (`src/precis/store/_material_ops.py:213-242`,
  exercised end-to-end by `tests/test_material.py`, 720 lines) issues an
  explicit-column `INSERT INTO material_values (...)` that never names
  `literal`. Once `material_values` is a rename-projection view over
  `measures` with `measures.literal NOT NULL` and no stated default, that
  INSERT fails with a NOT NULL violation on every existing call site —
  contradicting AC1 ("`tests/test_material*` ... pass unchanged"). The item
  needs to state a default/backfill value for `literal` on rows written
  through the old view (a real placeholder, since "the exact reported
  string" has no honest synthetic value) or an INSTEAD OF trigger — not
  left to the coder to invent.
  **[resolved]** — In scope item 1 now specifies the `INSTEAD OF INSERT`
  trigger and keeps `literal NOT NULL`; also added to Target + blast
  radius.
- **blocker: the 0-row claim is stale and, if wrong, has no backfill story.**
  The "`material_values` holds 0 rows" claim traces to a single
  2026-08-17 prod audit cited in
  `quest-data-table-and-formula-discovery.md:14-16` — six weeks old as of
  this review (2026-09-27) and unverifiable from a worktree. "Target +
  blast radius" already says to check the prod ledger for a migration
  *number* collision before numbering, but not to reconfirm the row
  *count* is still 0 before running a rename+view migration whose safety
  (per the finding above) currently assumes zero pre-existing rows needing
  a `literal` backfill. Re-check the count against prod before this runs;
  if nonzero, the item needs a backfill rule for `literal` on legacy rows.
  **[resolved]** — Target + blast radius now requires a pre-migration
  `scripts/prod-psql` recount, and the `literal` backfill rule from the
  finding above covers a nonzero result.

  **Measured 2026-09-28** (read-only prod query, all five tables present):
  `material_values` 8 rows, `material_properties` 24 (17 core / 7
  proposed), `component_spec_values` 68 rows, `component_specs` 30 (28
  core / 2 proposed), `component_categories` 13 rows — superseding the
  2026-08-17 audit's "0 rows" everywhere it's cited in this item. The
  premise this item opened with (an empty table, backfill theoretical) is
  dead: 8 real rows means the `literal` backfill rule in scope item 1 is
  load-bearing on this item's own first run, not a theoretical safeguard.
  Because these numbers move, the pre-migration recount in Target + blast
  radius stays required — this is a snapshot, not a substitute for it.
- **advisory: AC3's "pilot's format-v4.json fixture rows" cites a format
  spec, not a dataset.** `paper-extraction-pilot/format-v4.json` is
  tracked and present in this worktree (correcting any assumption that
  it's untracked or lives only in another tree) — but it is a field-shape
  spec (`purpose`/`reader_duties`/`top_level` schema), not example data;
  none of AC3's literal values (`9.6 ± 1.7`, `550–575`,
  `9.25 mg h⁻¹ mgcat⁻¹`, …) appear in it or anywhere else in the tracked
  pilot directory (real round outputs with quoted source text are
  deliberately excluded by `paper-extraction-pilot/.gitignore`'s
  allowlist). AC3 is still buildable — the values are spelled out inline
  in the AC itself — but should say these are hand-built fixture rows in
  the format-v4 *shape*, not rows sourced from that file, so a coder
  doesn't go looking for pre-built examples that don't exist.
  **[resolved]** — AC3's lead sentence now says the rows are hand-built,
  inline, in the format-v4 shape.
- **blocker: `primary_link_id` has no precedent and the obvious
  implementation silently collides evidence on a shared chunk.**
  `primary_link_id` appears nowhere else in the tree (grepped) — this is a
  new pattern, presumably built on `add_link`
  (`src/precis/store/_links_ops.py:274`): insert the link inside the same
  `tx()` as the measure row, then set `primary_link_id` to the returned
  `link_id` — no deferred constraint is needed since `links` never
  references back to `measures`, so the ordering itself isn't the hard
  part. The hard part: `add_link`'s dedup key is `(src_ref_id,
  src_chunk_id, dst_ref_id, dst_chunk_id, relation)` and does **not**
  include `meta` (where `span`/`anchor_scheme` live). Two different
  measures anchored to the same chunk — this item's own stated common
  case, "~2 spans/result," and AC3's "table-recipe row" — would collide on
  that key and both end up pointing at ONE link row carrying only the
  *first* measure's span, silently misattributing the second's evidence.
  None of the stated ACs exercise two measures sharing one chunk, so this
  ships gate-green and wrong. The item needs to say how measure-evidence
  links avoid `add_link`'s dedup (a non-deduping insert path, or a key
  that includes the span) before a coder wires `insert_measure` to it.
  **[resolved]** — In scope item 2 (store ops) now specifies the
  non-`add_link` insert path and the same-`tx()` ordering; new AC4
  exercises two measures on one chunk with distinct spans.
- **blocker (external contradiction): `component_spec_values` and
  `reaction-kind-and-synthesis-cost.md` are unacknowledged siblings that
  undercut the "one number record" goal.** `component`'s own
  `material_values`-shaped fact table already exists and is live
  (`component_spec_values`/`component_specs`, migration
  `0093_component_kind.sql:79-128`, wired through
  `src/precis/store/_component_ops.py` and `handlers/component.py`) — it
  is a *separate* table from `material_values`, not a caller of it, so
  "handlers/component.py untouched (views)" is trivially true (component.py
  never read/wrote `material_values`/`material_properties` — confirmed,
  zero hits) rather than evidence the compat-view plan protects a real
  component call site. Meanwhile this item's own `subject_ref_id` table
  (line 46) lists "component" as a subject kind `measures` should cover,
  and the Motivation's whole premise is "one number record" shared by
  every consumer — but In-scope never folds `component_spec_values` in,
  and the sibling `docs/backlog/reaction-kind-and-synthesis-cost.md`
  (draft, same `model: opus`) explicitly plans to mint a *third* verbatim
  copy of the same shape ("`reaction_values` — `material_values`
  verbatim", citing `component_spec_values` as the precedent that doing
  this again is "the house pattern, not a new invention"). As scoped,
  component values never flow through `measures`, and the reaction item —
  unaware of this contract — is positioned to reintroduce every gap (no
  literal, one text `source_chunk` handle, no reference/tier/trust) this
  item exists to retire. Needs either an explicit "`component_spec_values`
  is a deliberate follow-on, here's why" note plus a cross-reference added
  to `reaction-kind-and-synthesis-cost.md`, or folding component in now —
  before this ships as "the contract both trees build against."
  **[resolved for v1 scope]** — `subject_ref_id`'s kind list and
  "Explicitly NOT in scope" now say plainly that component values do not
  flow through `measures` in v1 and that folding `component_spec_values`
  in is a named follow-on; `reaction-kind-and-synthesis-cost.md` now
  cross-references this item. The **scope call below is still open** —
  it decides which release folds component in, not whether it happens.
- **decision needed (Reto): fold `component_spec_values` into this
  migration, or leave it for a follow-on?** Two options: **(i) v1 ships
  without component, a follow-on item folds `component_spec_values` in
  later** — smaller blast radius, now the **fallback**: its cost is that
  it ships a shared record that 89% of real values do not use (measured
  counts below); **(ii) fold `component_spec_values`/`component_specs`
  into this same migration** — larger blast radius (touches
  `handlers/component.py`'s live call site, not just an untouched view) —
  **now recommended**, for the quantitative reason below. Under either
  option, `reaction-kind-and-synthesis-cost.md` should wait for `measures`
  to land rather than mint a third verbatim copy of the shape.
  **Decided 2026-09-30, Reto: (ii).** The In-scope bullet above now
  states the fold-in; the prod counts below are its reason.

  **Measured 2026-09-28** (read-only prod query, all five tables present):

  | table | rows |
  |---|---|
  | `material_values` | 8 |
  | `material_properties` | 24 (17 core / 7 proposed) |
  | `component_spec_values` | 68 |
  | `component_specs` | 30 (28 core / 2 proposed) |
  | `component_categories` | 13 |

  `component_spec_values` outnumbers `material_values` roughly 8.5:1 —
  68 of the 76 sourced-value rows in the system (about 89%) live in the
  table this item, as scoped, leaves outside `measures`. Shipping (i) as
  scoped would move the nearly-empty table (8 rows) into `measures` and
  leave the actually-used one (68 rows) outside it, which falsifies this
  item's own "one number record" premise (Motivation) in practice on the
  day it ships, not eventually. That's the reason to recommend (ii) over
  (i), not a symmetry argument.

  **New evidence for this decision (Reto asked 2026-09-28: "is material
  just a list of things with a type, so we can generalize it for other
  enum-like things? can we link to materials?").** Checked: a `material`
  ref carries **no** type — it is a flat list of entity refs, and there is
  no `material_categories` table anywhere in the migrations. The type layer
  exists exactly once in the tree, on component: `component_specs` is
  `material_properties` copied verbatim **plus** `category_id REFERENCES
  component_categories(category_id)` (that table being `category_id / name
  / status core|proposed / description`). That single column is the whole
  difference between the two registries.

  So the missing generalisation is not a type on the *subject* but a
  **scoping layer on the registry** — which measurands are legal for which
  class of subject (`NULL = universal`, as component already does it). If
  `measurands` inherits a generic `subject_class` (component's
  `category_id` lifted, with its own `core`/`proposed` tiering), option
  (ii) stops being extra blast radius and becomes the reason to do it,
  because `component_categories` generalises into the one table both kinds
  read.

  **Under (ii), the registries are a head start, not greenfield.** The
  initial `measurands` table is not created empty and populated later —
  it is `material_properties` (24 rows: 17 core / 7 proposed) merged with
  `component_specs` (30 rows: 28 core / 2 proposed): 54 rows total, 45 of
  them already `core`. `component_categories`' 13 rows become the initial
  `subject_class` vocabulary (component's `category_id` lifted, `NULL` =
  universal, per the scoping paragraph above). This makes the migration a
  **merge of two curated registries**, not a create-and-populate step —
  and a merge needs a check the create-only version didn't: the two
  source registries' primary keys (`material_properties.prop_id`,
  `component_specs.spec_id`) must be checked against each other for slug
  collisions before both land in one `measurand_id` namespace. If (ii) is
  chosen, that check is a migration step (in scope item 1) and its own
  acceptance criterion (AC9), not left for a coder to discover at merge
  time.

- **Enum-like vocabularies do not need new machinery — three-way rule.**
  Recorded so a later pass does not add a fourth mechanism. (a) A small
  closed set, validated at write, that never needs its own page → a
  **closed tag axis** (`STATUS:`, `ATTEMPT:`). (b) The legal values of one
  measurand → **`allowed_values` jsonb** with `value_type='categorical'`,
  which both registries already carry. (c) Something you want to attach
  evidence to, describe, measure, or link from → **a ref**, and link to it;
  since a `material` *is* a ref, every relation in the registry already
  reaches it, and `measures.subject_ref_id` already accepts it. Prefer (c)
  over minting an enum column: it is how `concept` already works.
- **[decided 2026-09-30, product-plan review]** Reto's design notes'
  `origin` axis maps onto `tier`, no new values: `reported` → `measured`,
  `refit` → `computed` with `derived_from` = the curve row
  (`class-lattice-similarity-spaces-and-laws.md` §4), `derived` → `derived`,
  `imputed` → `derived` with the generator in the structured writer
  provenance (gap 2 above). Their `measurement_arg` (position, entity) is
  the `subject_selector text` column that `se-region-property-layer.md`
  adds so a design's *region* can be a subject; decide the grammar when
  this item ships (open question there). Participant roles are a separate
  axis from `direction` and the condition `role`
  (`class-lattice-…` §3). The one-paper ownership rule for `experiment`
  is widened for first-party runs by `first-party-experiment-records.md`
  (a run owned by a job / quest tick / se design), not here.
