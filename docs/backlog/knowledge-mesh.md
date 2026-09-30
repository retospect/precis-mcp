---
status: draft
title: Knowledge mesh — a persisted measure record on hubs, a generic graph walk, numeric-conflict disputes, and a per-quest mesher pass
prio: high
model: opus
blocked-by: measures-substrate
---

# Knowledge mesh — a persisted measure record on hubs, a generic graph walk, numeric-conflict disputes, and a per-quest mesher pass

## Motivation / why

Reto, 2026-09-27 (Bootstrap design session): "I would add a literature
mesher job; this 14-paper set is very sparse. How do we add a regular
literature / finding / experiment data mesh network? We want results,
yields, concepts etc. into a graph the system can navigate … find
neighbours 2 levels out that match semantically … use this cross-linked
memory for other things also (skills, memories) in a DRY way."

**What the graph already is.** One property graph in Postgres, four
tables: `refs` (a node of any kind, `meta` JSONB, handle), `chunks` (a
ref's body; `ord<0` cards; embeddings in `chunk_embeddings`), `links`
(`src_ref/src_chunk → dst_ref/dst_chunk`, `relation` from the closed
`relations` registry, free `meta`), `tags` (closed axes). A finding hub
(`fi<id>`) is a ref with a `finding_body` chunk plus evidence-edge rows
(`establishes`/`corroborates`/`contradicts`/`disputes`) that may anchor
a passage via `src_chunk_id`. Memories use the same `links` table and
auto-link `[handle]` mentions. So the mesh is not a new store; it is
three missing affordances on the existing one.

**What is missing, measured:**

1. **No persisted quantity.** A hub says "peak QY 68 % at 290 K" as
   prose + quote. Nothing can ask "best placement accuracy in nm over
   every hub serving capability qu453869". The materials frontier
   assembles `Candidate.measures` at read time from `struct_runs` +
   `structure.meta` (`quest/frontier.py::_candidate_from_structure`);
   a prod audit found 0 structures with `meta.params` and
   `material_values` at 0 rows (`quest-data-table-and-formula-discovery.md`).
   The Bootstrap spec had to invent a hand-written `meta.supply` for the
   same reason (`bootstrap-roadmap-quest.md`). Two consumers, no record.
2. **No generic walk.** `view='links'` is one hop; `inbound_chase`,
   `hub_refine`, `reweight.py`'s serves-DAG are each an ad-hoc traversal.
   There is no "from X, depth N, filter relation/kind, rerank by
   embedding similarity".
3. **Cross-linking exists only as dark, single-purpose passes**:
   `hub_refine` (corroborates/disputes via ANN + citation follow),
   `conflict_search` (negated-paraphrase ANN → disputes), `inbound_chase`,
   `chase_trigger`, `paper_rank`, `dream_agent` (memories) — all
   `OFF` by default, none numeric-aware, none quest-scoped.

**Adjacent work this item leans on, not duplicates.** The
`paper-extraction` worktree (Reto's "another branch works on this
machinery") is `corpus-quantitative-extraction.md`: a reader emits
anchor-bound *bindings* (measurand, exact literal, `value_form`,
`reported_unit`, condition sets with roles, scheme-versioned sentence
anchors, intra-paper conflicts), a materializer expands them, and
integration "goes through the existing findings/nanopub path". Its open
question — *is a chunk-level extraction a new kind or a finding
subtype?* — is answered here: **a materialized result becomes a hub +
one `measures` row + one passage-anchored evidence edge.** Cross-paper
measurand identity is explicitly out of that item's scope; this item
owns the thin alias map that a capability axis needs.

## Design

### 1. The `measures` record — one home for every number

**Owned by `measures-substrate.md`** (ships first): `material_values`
generalised into `measures` — `subject_ref_id` any ref (paper by default;
hub only after a result is promoted to a claim; structure; se design),
`literal` NOT NULL with parsed value/err/low/high/text derived and
nullable, `value_form` incl. `categorical`, `reference` state as a
column, `tier` measured/computed/derived/asserted with NULL = could not
establish, `trusted`, per-condition anchors inside `conditions`,
evidence as N `links` rows (one required primary for `measured`) whose
`meta` carries `anchor_scheme` + `span`, `meta.source_caveat`; registry
`measurands` with reference states, convert rules and aliases keyed on
(measurand, reference, convention). This item **consumes** that table.
Decisions from the paper-extraction session review (2026-09-27) are
recorded there, not repeated here.

**Adapters (no consumer rewritten):**

- `quest/frontier.py::_candidate_from_structure` merges
  `store.measures_for(structure_id)` after its `struct_runs`/meta
  harvest; existing keys win, `trusted=False` rows are excluded exactly
  as `barrier_trusted` is today. Harvest passes that today stamp
  `structure.meta.barrier` also insert a `computed` measure row; the
  meta stamp stays for one release, then goes.
- Capability **supply** = `best_measure(key, serving=<capability>)` for
  each rubric key — subject may be a paper, hub or structure serving the
  capability at any depth; sense from `rubric_objectives`; rows with a
  differing `reference` compared only through a `convert` rule.
  `bootstrap-roadmap-quest.md`'s `meta.supply` stays in v1 (its final
  2026-09-28 ruling); this adapter's supply-from-`measures` read is the v2
  widening that body defers, not built here.
- `view='series'` gains a `measures`-backed mode for non-structure rows,
  grouping by `conditions` names instead of `_SERIES_AXES`.
- Intra-paper conflicts the reader binds land as `disputes` edges with
  `meta.scope='intra-paper'` (§3); a source-stated reliability caveat is
  `meta.source_caveat` on the row, never a dispute.

### 2. Navigation — extend the eye ladder, do not add a walk verb

**Correction (2026-09-27, after reading the code):** the fisheye exists.
`workers/working_set.py` defines the extent ladder `kwd < summary <
verbatim < fisheye < fisheye+1hop`, eyes with persistence
(`transient`/`normal`/`pinned`) and provenance, a per-tick JSONB snapshot,
decay + `crunch`. `utils/eye_render.py::render_eye` renders per kind: tree
kinds (draft/plan) get the reading-order fisheye, doc kinds the
keyword-cluster fisheye, every other kind the **note eye** (title → gist →
body) plus, at `fisheye+1hop`, the link neighbourhood grouped by relation,
both directions, capped 8/group with a visible overflow. `utils/refeye.py`
is the reference ring with pins and claim groups.
`utils/working_set_render.py::render_working_set` composes N eyes into one
deduplicated context. Level 1 (policy-chosen eyes) is live on planner +
dream passes; wired on `draft` (`extent=`) and `finding` (`view=<label>`);
Level 2 (a `focus` verb, the render→act loop) is `fisheye-level2.md`;
advertising the affordance on every kind is that item's second section.
`precis_web/draft_eyes.py` already renders pens + eyes for drafts.

So the "universal front end" is the eye system, and this item adds rungs
and widens sets rather than a parallel verb:

- **`fisheye+2hop`** rung: second hop rendered as **counts per kind per
  relation** plus the top-k by card similarity to the origin (or to a
  query when one is given). The refeye docstring already reserves
  similarity for a separate **`+recall`** rung — build that rung, and let
  `+2hop` use it for its top-k; the structural leg stays deterministic.
  Closes the 2026-07-23 graph audit's finding 4 (no aggregate/fan-in
  graph view; `links_for` is one-ref/one-hop).
- **Multi-origin working sets = pins.** "Near all of these handles" is a
  `WorkingSet` with several pinned eyes composed by `render_working_set`;
  nodes demanded by more than one eye rank first. No new query. Closes the
  2026-07-23 graph audit's finding 3 (no two-ref intersection query).
- **Ring set widening — a decision, not a consequence.** `RING_RELATIONS`
  deliberately excludes structural relations; `serves`, `blocked-by` and
  `parent` are exactly what a roadmap neighbourhood needs
  (capability ↔ rung ↔ pathway). Add a `ROADMAP_RELATIONS` set used when
  the eye's *origin* is a roadmap quest or a rung, pinned by
  `tests/test_kind_totality.py` like the others. Do not widen the global
  set.
- **Card contract.** The note eye renders `title → gist → body`. Every
  kind must yield a non-empty gist: LLM-authored kinds via the lede rule
  (§5); ingested kinds via the summariser; structured kinds
  (structure/se/pcb/measure) via a deterministic gist from their data
  (check `utils/kind_facts.py` first — it may already be this). A registry
  test asserts a non-empty gist per kind on a fixture ref, parallel to
  the every-kind-has-a-help-skill test.
- **Wire `extent=`/`view=<label>` on every kind's `get`** — the
  `fisheye-affordance-generalize` section of `fisheye-level2.md`, promoted
  from "mechanical, optional" to a prerequisite here.
- **Browser focus page** = generalise `precis_web/draft_eyes.py` to any
  handle: pen tray, eyes with extents, click-to-refocus with breadcrumbs.
  Same renderer as the tick.

**Filter grammar (handoff Q3, decided 2026-09-29).** The mesh handoff
proposed traversal filters `role[:tag-path,...]` (e.g.
`derived_from:/axis/mechanism`) and `role!` for the reverse direction (e.g.
`specialises!` walks down to children). Ruling: this is not a walk verb; it
is the filter argument of the `fisheye+1hop` / `fisheye+2hop` rungs —
`relations=` (list of relation slugs, `!`-suffix = follow the inverse) and
`axis=` (a taxon slug under the `axis` start node from `term-taxonomy.md`,
widened by `under=`). Path wildcards map to facets: `/x/**` = `under=x`,
`/x/*` = `under=x, depth=1`. No glob parser. Relation registry:
`store/types.py::Relation`; per-relation grouping already exists in
`utils/eye_render.py::render_eye`.

**The tick's view is a working set, not a bespoke render.** The roadmap
body (`bootstrap-roadmap-quest.md`) places eyes the way planner/dream
passes do: the capability at `fisheye+1hop` (ROADMAP_RELATIONS), the
framing chunk and ledger chunk at `verbatim`, handles named in the framing
chunk `pinned`, plus a **changed-since-last-tick** block from `ref_events`
rendered first. Cost per deed goes on the quest health line and steers
cadence; no per-tick length penalty.

### 3. Argue — numeric conflict as a first-class dispute

Extend `conflict_search` (exists, OFF) with a **deterministic numeric
rule, no LLM**: two `measured`/`computed` rows with the same `key`,
`operating` conditions that match within tolerance, and values whose
intervals do not overlap → `disputes` edge between the two hubs with
`meta = {reason: 'value-conflict', key, a, b, conditions}`. The
`view='series'` contradiction row and the report's contradictions table
read these edges. Adjudication (`contradicts`) stays where it is —
nanopub/hub_refine — never filed by this rule. Intra-paper conflicts
the extraction reader already binds land as the same edge with
`meta.scope='intra-paper'`.

### 4. The mesher — one scheduled pass per active quest

A worker pass (`registry.py` service `quest_mesh`, OFF by default,
per-quest opt-in via `meta.mesh = {budget_usd, cadence_h}`), one action
per tick, chosen by the quest's thinnest capability axis (measure count
per key, from `measures`):

| state | action |
|---|---|
| axis has < N measures | **widen**: deep campaign `search(kind='paper', q=<axis statement>, queries=[aliases…], good=True)`; hits → stubs `serves` the pathway |
| held papers with no measure on this axis | **read**: mint a todo that queues the quantbind reader pass on the paper with the axis as the question (queued job via todo — never an in-process direct call, per the extraction session); rows land on the paper; the mesher re-ticks when the todo lands |
| hubs exist, their sources' citation neighbours unheld | **walk**: S2 `refs:`/`cites:` one hop from each hub's source paper; primaries a review points at → stubs |
| new measures since last tick | **reconcile**: run the numeric-conflict rule over the axis; bump `no-literature`/`thin-support` gaps |

Concepts ride along: a hub minted for an axis also `serves` the concept
named in the axis statement when one exists (`low-mastery` gap reads
it). Memories are untouched by the mesher; they are reachable by
`walk`.

### 5. The front end — one row shape, lede-first nodes

Reto, 2026-09-27: "a clever universal front end independent of the
underlying structure … nodes in a format so we can read the first
sentence, consecutive sentences are continuous refinement, or do we run
summarizers?"

**Split by author.** LLM-authored nodes (hubs, memories, framing chunks,
rung bodies, dossier chunks) follow the **inverted pyramid** and a
write-time lint enforces it: sentence 1 is the whole claim, standalone,
no bare handle, ≤ N chars; each later sentence adds exactly one
qualifier (condition · evidence handle · caveat · number). The card
embeds sentence 1 alone AND the block, so search matches the lede
cheaply and expands on demand. No summarizer runs on these. Ingested
papers keep the existing trickle summarizer (`llm_summarize`) for their
card. `measures` rows get a **deterministic** lede from the row
(`<key> = <literal> <unit> vs <reference> @ <operating conds> [tier]`),
no model call.

**The universal row** every verb returns (search · walk · gaps · series
· census): `handle · kind · lede · keywords · state/tier · path (walks
only) · more-cursor`. Expansion ladder via `more()`: lede → block →
full body. Same shape from a paper, a hub, a memory, a measure, a quest.

**Searches the LLM runs, mapped:** by meaning (exists) · exact token
(exists) · structure (walk, §2) · number (substrate range read) · set
("near all of these handles" = walk intersection) · contrast (series
over measures) · provenance (walk with fixed chain measure → edge →
chunk → paper) · gap (exists) · change-since (ref_events, exists).
Only structure, set, contrast are new, and they are one recursive
query with different filters.

Ships first within this item: the lint + the row shape are cheap and
make everything else navigable.

### 6. What this does to the catalysis quests

Directly better, no body change: stage 1 of
`quest-data-table-and-formula-discovery.md` ("substrate: a materialized
data table … `precis quest table <quest>`") is the `measures` table
plus the frontier adapter. Trusted barriers become `computed` rows with
`trusted=True`; the regression stage reads rows, not `structure.meta`.
The 0-rows `material_values` registry is superseded by `measures` with
`tier='measured'`.

## In scope

1. (moved to `measures-substrate.md`.)
2. Frontier adapter + harvest dual-write; capability supply-from-`measures`
   read, as a v2 widening once `bootstrap-roadmap-quest.md`'s `meta.supply`
   is superseded — not this item's v1.
3. (moved to `fisheye-everywhere.md`, 2026-09-30 — it depends on no
   measures work; this item's walk reuses its ladder.)
4. Numeric-conflict rule in `conflict_search`.
5. `quest_mesh` service with the four actions, per-quest budget.
6. Integration seam for the extraction worker pass: `materialized
   result → hub + measure + edge` via the existing findings approve
   path (the extraction item ships the reader; this item ships the
   landing).
7. Lede-first write lint on LLM-authored kinds; universal row shape +
   `more()` expansion ladder on search/walk/gaps/series/census.
8. Runtime docs: `precis-measure-help` (new), `precis-graph-help` (new),
   `precis-finding-help` (measures section), `precis-quest-help` (mesh
   meta, supply derivation).

## Explicitly NOT in scope

- The extraction reader, anchor scheme, recipes, escalation ranking —
  `corpus-quantitative-extraction.md` (the `paper-extraction` tree).
- A measurand ontology / cross-paper identity resolution beyond the
  per-capability alias list.
- Adjudication (`contradicts`), nanopub minting/signing, demotion.
- A graph database. Postgres + recursive CTE + pgvector is the graph.
- Turning skills into refs. Filed as `file-mirror.md` (2026-09-30).
- The roadmap tick body itself (`bootstrap-roadmap-quest.md`).
- The `focus` verb and render→act loop (`fisheye-level2.md`); this item
  extends the ladder those will drive.

## Acceptance criteria

1. `best_measure('placement_error_nm', serving='qu453869')` returns the
   best trusted row whether its subject is a paper, a hub or a structure
   serving the capability at depth 1 or 2.
2. A structure with a trusted relax gains a `computed` row; the frontier
   for its quest is byte-identical to before the change on the existing
   `tests/test_quest_frontier` fixtures (dual-write invisible), and
   `precis quest table <quest>` lists the row.
3. `get(kind='finding', id='fi<x>', view='fisheye+2hop')` renders the 1-hop
   groups as today plus a 2-hop counts line and ≤k recall hits; a rung
   origin shows its `serves`/`blocked-by` neighbours, a memory origin does
   not; every registered kind yields a non-empty gist on a fixture ref.
4. Two hubs with same key, matching operating conditions, non-overlapping
   intervals → one `disputes` edge with `meta.reason='value-conflict'`;
   overlapping intervals or a differing operating temperature → none.
5. One `quest_mesh` tick on a fixture quest with one thin axis performs
   exactly one action, logs it as a `result` entry, and stays under the
   quest's `meta.mesh.budget_usd`.
6. (v2 widening, not this item — `meta.supply` stays per
   `bootstrap-roadmap-quest.md`'s final 2026-09-28 ruling; see the
   2026-09-29 correction below. v2's AC: `view='tree'` renders supply from
   `measures` when no `meta.supply` is present.)
7. Skills: `search(kind='skill', q='walk neighbours two hops semantic')`
   returns `precis-graph-help` top.

## Target + blast radius

- `src/precis/migrations/<next>_measures.sql`; `src/precis/store/`
  (measures ops); `src/precis/handlers/finding.py` (measure put/view);
  new `src/precis/handlers/graph.py` (walk) + `cli/graph.py`;
  `src/precis/quest/frontier.py::_candidate_from_structure`,
  `results_table.py` (series over hubs); `src/precis/workers/registry.py`
  (`quest_mesh`, `conflict_search` numeric rule); harvest passes that
  stamp `structure.meta.barrier`; `src/precis_web` neighbourhood panel.
- Docs/skills as listed. `docs/reference/schema.md` regen.
- Prod: dark until `PRECIS_QUEST_MESH_ENABLED` + per-quest `meta.mesh`.

## Open questions / decisions log

- **Measure on the hub vs its own kind.** Decided (revised after the
  paper-extraction review): a row on `measures` owned by the **paper**
  by default, promoted to a hub only when the result becomes a claim;
  not a ref kind — no handle, no embedding, no card. The extraction
  item's "new kind or finding subtype" question resolves to *neither*.
- **Where does the reader run** — decided by the extraction session:
  queued job via a todo. The mesher's `read` action mints the todo.
- **Skills in the walk.** Skills are files with no `links` rows. Either
  a read-only ref mirror (one ref per skill, chunks = sections, so
  `walk` and `related-to` reach them) or leave skills out. Reto asked
  for DRY reuse across "skills, memories"; recommendation: mirror,
  as a separate small item after the walk verb exists, not here.
  Filed 2026-09-30 as `file-mirror.md` (read-only mirror first; native
  authoring is Reto's pending ruling).
- **Tolerance for "conditions match".** Start with: same `operating`
  condition names present on both, numeric values within 10 % or
  identical categorical; tighten after the first real conflicts land.
- **Budget.** The mesher's `widen` action runs a deep campaign (agents
  fan out). Per-quest `budget_usd` is mandatory, default absent ⇒ the
  service skips the quest.
- **Correction, 2026-09-29:** §1's frontier-adapter bullet and In-scope
  item 2 previously said this item drops `bootstrap-roadmap-quest.md`'s
  hand-written `meta.supply`. Wrong — that body's decisions log rules
  `meta.supply` stays in v1 (revised twice, final 2026-09-28); the
  supply-from-`measures` read is the v2 widening it defers. Fixed both
  places.
