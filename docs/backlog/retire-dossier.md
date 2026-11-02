---
status: draft
pillar: quests
prio: high
title: Retire the dossier — the quest's typed edges carry the synthesis, one memory card per experiment
---

# Retire the dossier

Reto 2026-11-01/02 (gr549581, gr553318 folded in): one `memory` card per
experiment, under its direction todo — §Experiment memory cards. Passes:
1 this design (done 2026-11-02), 2 the card writer incl. auto-commit, 3
conversion of the 25 dossiers (qu164903 first, dry run to Reto), 4 the
pathway page repoint. Build on HOLD until Reto rules on §Open for Reto.
Shipped so far: slices 1–2 (`quest/directions.py`, the relation pin) and the
quest-page dossier block removal (gr541346); the tick still rewrites the
dossier (dr164905) on every unflagged quest.

Reto 2026-10-24 (gr510505): "retire the dossier". Refines Reto 2026-10-01
(`quest-graph-as-dossier.md`): "we want a graph that 'belongs to' the quest,
and it 'is' the dossier. We can linearize it with a writer agent, but we
manage the things in the mesh, with a hierarchy of 'belongs to quest' or
something with the quest node at apex." This spec answers that item's three
open questions (relation, hierarchy, linearisation trigger). A second
2026-10-24 rule, relayed from Reto's chat session `chat-99`: evidence
(findings, measures, papers) `serves` the narrowest todo or question it
answers, never the quest directly, and the existing direct quest links get a
one-off re-home (migration step 6). The reader of the result is an LLM
(§Decisions, goal ruling): the quest's graph must be cheap for an agent to
traverse; a human page is secondary.

Retires `src/precis/quest/dossier.py`'s storage: the `draft` linked
`dossier-of` to a quest, its rewritten narrative, its pinned ledger
(`meta.pinned='ledger'`/`'ledger-node'` chunks + `ATTEMPT:` chunk tags),
its pinned `frontier-tree`, `dialectic*` and `capability-ledger` chunks, and
the unused `paper-of` projection. Deletes weave (§Decisions Q8).

## Target shape

| Today (dossier) | After |
|---|---|
| `dossier-of` draft per quest | none; no dossier tag, no bundle node |
| pinned ledger node | direction todo, `part-of` the quest (root) or nested under a strand by `parent_id` |
| `ATTEMPT:<status>` chunk tag | todo `STATUS` (mapping below) |
| `ledger_do_not_repropose` over the forest | query over the quest's direction todos in `done`/`won't-do`, same render rules |
| top-level ledger nodes with children | strand todos — the only index nodes (§Quest navigation) |
| finding/measure/paper `serves` the quest | `serves` the narrowest direction or strand todo it answers; the quest only when none fits (step 6) |
| narrative chunks, LLM-rewritten each tick | key ideas minted as findings before retire (step 0); prose archived (step 2); the tick writes one WORM synthesis entry per tick (Q1) |
| `frontier-tree` pinned chunk, rewritten each tick | `frontier.render_frontier_tree` rendered on the quest page and `view='frontier'` |
| dialectic blocks | hypothesis findings + `supports`/`disputes`/`tests` edges (`supports`/`disputes` minted at apply time, `dossier.py::_DIALECTIC_EDGE_RELATION`; `tests` by `rulings.mint_measurement_rulings`) |
| `capability-ledger` pinned chunk (roadmap root) | rendered on read from `roadmap_ledger` |
| `paper-of` draft (0 links on prod) | none; an export is a writer-authored draft that `serves` the quest (Q5) |
| weave body (0 quests on prod) | deleted; papers reach the graph through `hub_refine` (Q8) |
| a candidate's why/expected/result/next: scattered over narrative + logbook, no edge to the structure | one `memory` card per candidate structure, `part-of` its direction, `serves` the quest (§Experiment memory cards) |
| a logbook entry names `[st…]` as plain text | chunk-scoped `cites` edge entry → structure, written by `logbook.append_entry` (gr553318) |

**Membership.** `serves` keeps its meaning (work/knowledge that advances the
quest: 3528 papers, 1112 findings, 682 structures, 44 todos serve quests on
prod today), with Reto's narrowest-target rule: evidence serves the direction
or strand it answers. `part-of` is new on quests and means "a direction of
this quest's attempt tree"; it is what separates a direction from a work
todo that `serves`. A memory card is `part-of` a direction todo, never of
the quest (the direction readers filter `kind = 'todo'`, so a card is never
a direction: `directions.direction_ref_ids`, `store/_todo_sql.py::
direction_todo_sql`).

**The `part-of` door.** Migration 0095 registered `part-of` ↔ `contains` for
components ("Source component is structurally part of target component.")
before 0180 added the constraint columns; 0180 sets `contains` acyclic +
transitive and touches no other column of either row. So both rows have
`domain_kinds`/`range_kinds` NULL, and `check_relation_constraints`
(`utils/link_tag_ops.py`) checks a `part-of` write as `contains` with its
ends swapped: todo→quest passes the kind check, and the acyclic walk refuses
a quest that would end up inside its own direction. No constraint change.
Slice 2 pins it: `test_part_of_todo_to_quest_passes_constraints` in
`tests/test_relation_constraints.py` (todo→quest accepted; quest→todo after
it refused as a cycle). If that test fails, the fix is a constraint-row
migration landed through builder-ce before slice 5 writes any edge. The
component wording of the description only shows in a refusal message; slice
10's migration rewrites it kind-neutral. `Store.part_of_descendants`
(`store/_refs_ops.py`) already walks this tree, both orientations.

**Strand.** A todo `part-of` a quest that has child todos. A direction is
any todo in that tree. A root with no children is a direction directly
`part-of` the quest. The migration mints no synthetic strand; the tick may
open an umbrella strand (§Quest navigation, §Decision).

**Status mapping** (todo `STATUS` vocabulary is closed and trigger-enforced,
`store/types.py::_KIND_STATUS_VALUES`, migration 0176:
`open/doing/blocked/done/won't-do/paused/auto-timeout`; it has no
`tried`/`ruled-out`):

| ledger `ATTEMPT:` | todo `STATUS` | do-not-repropose |
|---|---|---|
| `open` | `open` | no |
| `active` | `doing` | no |
| `tried` | `done` | yes |
| `ruled-out` | `won't-do` | yes |
| `idea` (registered, never written; 0 on prod) | `open` | no |

Model-side (todo → the `open/active/tried/ruled-out` the prompt speaks):

| todo `STATUS` | renders as | do-not-repropose |
|---|---|---|
| `open`, `blocked`, `paused` | `open` | no |
| `doing` | `active` | no |
| `done`, `auto-timeout` | `tried` | yes |
| `won't-do` | `ruled-out` | yes |

`blocked`/`paused` are set only by a human or agent tagging the todo (the
tick never writes them); they stay on the open list so the model sees the
direction and does not re-add it. `auto-timeout` needs `meta.auto_check`,
which no direction carries; it is mapped only so the reverse map is total.
The four statuses the ledger uses round-trip exactly (`idea` is never
written), so the retire tool loses nothing and the queue predicates
(`precis.todo.TERMINAL_TODO_STATUSES`) stay correct. `precis.quest.directions`
maps at the boundary.

**Rotation fence.** Direction todos are not work: they must never enter
`view='doable'`/`'queue'`, the nursery, hub counts or `gaps.quest_momentum`'s
`open_todo_servers`. The fence is "reachable from a quest by `part-of` +
`parent_id`", applied in `precis/todo/views.py` and the nursery sweep.

## Experiment memory cards (gr549581; gr553318 folded in)

Reto 2026-11-01: one `memory` card per experiment; an experiment is a
candidate `structure`, not a pathway (prod 2026-11-02: 219 of qu164903's
305 candidates have more than one pathway, max 9; they share one card).
The card holds why it ran, what was expected, what was dispatched, the
result, the analysis, the next ideas and its lineage — every line a
verbatim quote with its source handle; what no source records reads
`not recorded`, never inferred. Todos stay the direction tree (status,
do-not-repropose). The structure's fisheye reaches its card in one hop;
the card reaches its logbook entries, pathways, the cards before and after
it, and the quest. A card never links a draft (gr549580). The reader is an
LLM: a card is one `get`, its sections are `~` selectors.

### Card shape

| Field | Where | Value |
|---|---|---|
| title | `refs.title` (≤ 80, `handlers/memory.py::_TITLE_MAX`) | `<structure title>: <outcome line>`, conclusion first (precis-firstline-help); recomputed on every Result line |
| hook | `meta.hook` | the outcome line alone, e.g. `barrier measured, trust gate refused (anchor:none)` — the index and recall line |
| body | the `memory_body` chunk | seven `##` sections, fixed order: `Why`, `Expected`, `Plan`, `Result`, `Analysis`, `Next`, `Lineage`; `_SECTION_HEAD_RE` makes each one `me<id>~<section>` |
| line | body | `"<verbatim quote>" [<source>]`; sources: `[ql<chunk id>]` a logbook entry (`handle_registry.CHUNK_CODES['quest']`), `[dc<id>]` a dossier chunk (conversion only), `[pw<id>]` a pathway's stored measure, `[plan: <created_by>]` a dispatch the code made (`run_compute_step:new-candidate`, `promote_tiers:screening→neb`, `request_rerun by=agent`, …) |
| empty section | body | the single line `not recorded`. An auto-committed candidate's `Why` reads `not recorded — auto-committed after the model stalled N tick(s) [ql…]`, quoting the ladder's `decision` entry (Boron, st549017: prod entries 1667/1671) |
| `Lineage` | body | `from [me…] (st… derived-from st…)` and `to [me…], …`, mirroring the `derived-from` edges; `not recorded` when the structure has none |
| tags | open tags; `SPACE:research` stays the memory default (`default_tags_on_create`) | `card:experiment` — the marker every reader filters on; `quest:qu<id>`; `outcome:<pending\|measured\|trust-refused\|ruled-out\|graduated>` mirrored from the structure (`meta.barrier` / `meta.barrier_trusted` / `ruled-out:*` tag / `needs-experiment` tag) |
| meta, system-stamped | `refs.meta` | `card_structure`, `card_quest`, `card_sources` (chunk ids already quoted — idempotency), `card_rev`, `converted_from` + `converted_at` (pass 3). `_validate_meta` lets agents write `hook`/`type` only, so these stay system-owned: the writer uses `store.insert_ref` + `stamp_ref_meta` (the `workers/dream_consolidate.py` pattern), never the handler |
| never | | a `DREAM:` tag or `auto_refresh_days` — a card neither decays nor consolidates (`_dream_stamp_defaults`, `dream_consolidate`) |
| size | | the fisheye shows 4,000 chars of a body, `get` all of it. Past 12 kB the writer moves the oldest `Result`/`Analysis` lines into one child memory `part-of` the card (precis-memory-help summary/detail) and leaves `+N earlier lines [me…]` |

Agents edit a card through the normal memory doors (`edit`, `tag`) —
`Analysis`/`Next` are theirs to extend; the writer only appends and never
rewrites an agent's line. Quotes are verbatim, so the attribution gate
(`AUDIT:ungrounded-number`, `handlers/_attribution.py`) stays quiet.

**Where each line comes from** (the routing is by the entry's
`meta.entry_type`, `logbook.ENTRY_TYPES`; a dispatch is a code call, not
an entry):

| Source | Section |
|---|---|
| the proposal's `hypothesis` entry (`_stage_apply`; the ladder after pass 2) | `Why`; its `(expects: …)` tail → `Expected` |
| dialectic `experiment` entry naming the structure (`dossier.apply_dialectic_op`, `predicts:`; after retire the direction todo with `meta.hypothesis_id`) | `Expected` |
| `run_compute_step` relax + autocatpath dispatch, `promote_tiers`, `redispatch_candidates`, `request_rerun`/`apply_reruns` (with the model's `reruns[].why`), `harvest_measures` amnesty/stuck-seed/infra-retry redispatches | `Plan` |
| `result`, `dead-end`, `milestone` entries; `_stamp_preflight_dead_end`; `graduate_frontier` | `Result` (+ the `outcome:` tag) |
| `observation`, `note`, `reflection` entries | `Analysis` |
| `decision` entries; a `hypothesis` entry naming the structure after it exists (the experiment it spawned) | `Next` |
| `cost` entries | ignored |

The proposal schema gains one optional field, `expected` (one sentence,
the pre-registered outcome), folded into the hypothesis entry text as
`(expects: …)` the way `predicts:` is folded today — the only prompt
change; without it `Expected` is `not recorded` for every plain proposal.

### Links

| Edge | rel | Note |
|---|---|---|
| card → direction todo | `part-of` | the card's home; zoom-out from a card follows it (`refeye.UP_RELATIONS['memory']`); `view='members'` / `under=` on the todo list the card (`Store.part_of_descendants` has no kind filter). Resolved from the proposal's new `direction` field (`td` handle or quoted text, `directions._resolve_node`), else the dialectic hypothesis's experiment todo (`meta.hypothesis_id`), else absent until step 6 re-homes it (rule b on the structure title, rule a through its pathways' `tests` edges) |
| card → quest | `serves` | always; the structure carries the same edge, so `directions.quest_evidence` and the quest's Roadmap ring see the card |
| card → candidate structure | `related-to` | the bridge the pathway↔structure edge already uses (`compute._link_pathway`); renders under Notes & links on both fisheyes today. `RING_GROUP_KIND_RANK` gains `"Notes & links": ("memory",)` so the card lists before the pathways |
| card → each pathway | `related-to` | added when `_link_pathway` runs |
| card → the card before it | `derived-from` | the structures' lineage edge (`compute._link_parent_if_present`, `StructureHandler.derive`) mirrored one level up; the next cards read as `derived-into` (`_INVERSE_RELATIONS`); renders under Argument |
| logbook entry → structure | `cites`, chunk-scoped | `add_link(src_ref_id=<quest>, src_pos=<entry ord>, dst_ref_id=<st>)`; §Logbook edges |
| card → draft | none | gr549580 |

**Memory `part-of` todo is allowed today.** `part-of`/`contains` is one
constraint row: `domain_kinds`/`range_kinds` NULL, `contains` acyclic +
transitive (prod `relations`, 2026-11-02; migration 0180 touched only
`contains`), and `check_relation_constraints` checks a `part-of` write as
`contains` with the ends swapped, so the kind check passes for any kind
and only a cycle refuses. Prod has 0 memory→todo `part-of` rows, so the
case is untested in the wild; slice 2b pins it next to
`test_part_of_todo_to_quest_passes_constraints`
(a new pin in tests/test_relation_constraints.py, to be added: memory part-of todo passes the constraints).
No rule change. The two guards on the door are irrelevant here:
`guard_taxon_hierarchy` fires on `specialises`/`generalises` only;
`_machine_edge_admission` (`store/_links_ops.py`) gates machine writers
with a model identity, and the writer uses the same `store.add_link`
door `_link_pathway`/`_link_parent_if_present` already pass — slice 2b's
test runs the writer under the tick's write context to prove it.

### Writers

Every path that mints a candidate, measures one or names one must write
or update its card. `quest/cards.py` (new) owns the three operations —
`ensure_card(store, quest_id, structure_id, *, why=…)`, `append_line(…,
section=, text=, source=)`, `set_outcome(…)` — each one transaction on the
card ref, idempotent by `meta.card_sources`.

| Path | Has at that moment | Lacks | Card op |
|---|---|---|---|
| `tick.py::_TickRun._stage_apply` — writes `candidate: <name> [buildable] — <rationale>` as a `hypothesis` entry per proposal | name, rationale, `parent`, structure spec, the entry's ord | the structure id (minted two stages later), the direction | stash `{name: entry ord}` in the checkpoint (`self.st['proposal_entries']`) so compute can quote it; nothing else |
| `compute.py::run_compute_step` → `_ensure_candidate_detail` — the only minter (`ensure_candidate` has no other caller in `src/`) | proposal, the new or existing structure id, `was_duplicate`, the resolved `parent` structure, the quest | the hypothesis entry's ord unless passed; `expected` unless the schema gains it | **`ensure_card`** on a fresh mint: `Why` quotes the hypothesis entry `[ql…]`, `Plan` the relax + autocatpath dispatch, `Lineage` + `derived-from` to the parent's card; on `was_duplicate` or a symmetry duplicate: one `Why` line quoting the `duplicate proposal` observation (`_note_duplicate_proposal`, `_note_symmetry_duplicate`) on the existing card. Also mints the chunk-scoped `cites` edge from the hypothesis entry to the new structure (gr553318's missing "why" edge). `run_compute_step` takes `entry_ords: dict[str, int]` from both callers |
| `tick.py::_TickRun._stage_ladder` → `_commit_rung` → `run_compute_step` — the **auto-commit**: a re-prompt rung's proposals are materialised with no `hypothesis` entry; the only record is the `decision` "committed after re-prompt: <names> — model stalled N tick(s)" written after the fact. Boron (st549017) landed this way 2026-11-01: relax result 1666, decision 1667, no hypothesis entry | the rung's proposals (name, rationale, `expected` after pass 2), the stall count, the tier | a hypothesis entry; the direction | before `run_compute_step`, log each rung proposal as a `hypothesis` entry tagged `[auto-commit]` in the text (same shape as `_stage_apply`) and pass the ords; `ensure_card` then quotes it; `Why` opens with `not recorded — auto-committed after the model stalled N tick(s) [ql<decision>]` and the rationale quote follows. Without this, every stalled quest keeps producing card-less runs |
| `compute.py::harvest_measures` — relax result, autocatpath result, trust-gate refusal, nonphysical/unrelaxed warnings, seed diagnostics, `dead-end` + `ruled-out:*`; `_link_pathway`, `_link_refines`; redispatches (`created_by='harvest_measures:*'`) | structure id, pathway id, measures, the exact entry text | nothing for `Result` | `append_line(Result, <entry text>, [ql…])` per entry it writes, `related-to` card → pathway, `set_outcome`, title/hook recompute; redispatches → `Plan` |
| `compute.py::_stamp_preflight_dead_end` | structure id, reason | — | `Result` + `outcome:ruled-out` |
| `compute.py::promote_tiers`, `redispatch_candidates`, `request_rerun`/`apply_reruns` | structure id, tier, seeds, `created_by`, the model's `why` (reruns) | — | `Plan` lines |
| `graduate.py::graduate_frontier` | structure id, the `needs-experiment` deed | — | `Result` + `outcome:graduated`, `Next` quoting the graduation note |
| `dossier.apply_dialectic_op` (`experiment`, `predicts:`) today; the experiment direction todo after step 3 | hypothesis finding, experiment text, structure handles (`rulings._experiment_structures`) | — | `Expected` on each named structure's card; `rulings.mint_measurement_rulings` → `Analysis` line `ruling [fi…]` |
| `logbook.append_entry` — the one insert path (handler `entry=` and the tick) | quest id, chunk id, text, `entry_type`, `by` | the structure ids: parsed from the text | the catch-all: `extract_handles(text)` (`utils/mentions.py`) → for each `structure` handle, the chunk-scoped `cites` edge + `append_line` routed by the table above, `ensure_card` with `Why = not recorded` when the structure has no card yet. This covers every model-authored observation/decision/reflection and every human entry without touching their callers |
| `precis quest extract-cards` (pass 3) | the dossier, the logbook, the structure meta | — | §Conversion |

Pass 2 ships the writer before any retire apply: on an unflagged quest the
card gets `serves` quest + `related-to` structure/pathways + lineage, and
its `part-of` direction when `--rehome` (step 6) runs after retire.

### Logbook edges (gr553318)

Prod 2026-11-02: qu164903's logbook has 1,739 entries (observation 574,
result 516, decision 278, cost 175, hypothesis 103, dead-end 39, note 37,
reflection 17); 785 name a structure as `[st…]`, none carries an edge.
st478029 (Pd111-Ta-subsurf-1-vacancy-surf-colocated) is named by handle in
21 entries, by title in 28; its first title mention is entry 654, its
first handle mention 668, and 1 of its 2 proposing `hypothesis` entries
carries the handle — so the gripe's last question is answered: a proposal
entry is written in `_stage_apply` before `run_compute_step` mints the
structure, and names the proposal, never the id. The fix is the
`_ensure_candidate_detail` back-link above, not a prompt change.

- The edge: `cites`, chunk-scoped from the entry (`links.src_chunk_id`)
  to the structure, link meta `{'entry_type': …, 'by': …}`. Written in
  `append_entry` for every `structure` handle `extract_handles` finds;
  backfilled for the 785 by pass 3 (`--all`, idempotent on the unique
  tuple).
- The fisheye follows `cites` both ways incl. chunk-level edges
  (`eye_render._link_neighbors`), so a structure's ring lists its entries
  under Notes & links behind the card (the 8-per-label cap and
  `q='cites'` expansion apply; §Balanced TOC clusters a long one). The
  quest's own ring gains an outbound `cites` group under the same cap.
- The grouping gr553318 asks for (why / plan / results / analysis /
  lineage) is the card's sections, not a ring renderer: one `get` on the
  card reads them in order with their `[ql…]` sources. A `q='cites'`
  expansion on the structure stays the exhaustive list.
- Lineage: `derived-from` already links st478029 → st242611
  (`_link_parent_if_present`) and st517875 → st478029 when the proposer
  passed `parent`; the cards mirror it. A missing `parent` is `Lineage:
  not recorded`, never guessed from titles.

### Conversion (pass 3)

`precis quest extract-cards <qid>|--all [--apply]`
(`src/precis/quest/cards.py`, CLI `cli/quest.py`), dry run by default,
deterministic — no LLM, so nothing can be invented. Order: qu164903 dry run
to Reto; on his word `--apply` on qu164903; then `--all` dry run; then
apply on the other 24 dossiers of the 2026-10-24 census (25 live
`dossier-of` links on prod 2026-11-02, incl. the 6 retired quests — their
information stays relevant, Q6). Per quest, per candidate structure
(`compute._candidate_struct_ids`), one transaction:

1. Sources, in this order, each quoted as the sentence containing the
   mention (≤ 300 chars) with its handle: logbook entries naming the
   structure by handle or by exact title (the `[ql…]` lines, routed by
   the section table); dossier chunks naming it (narrative, ledger nodes,
   dialectic entries; `[dc…]` — the draft is soft-retired by step 5, the
   handle still resolves); the structure's own meta (`barrier`,
   `barrier_trusted`, `barrier_fidelity`, `ruled-out:*`, `params`) as
   `[st… meta]` `Result` lines; each `related-to` pathway's stored
   measures as `[pw…]`.
2. A section with no source → `not recorded`. B-subsurf-1's card reads
   `Why: not recorded — auto-committed after the model stalled 3 tick(s)
   [ql…]`, `Expected: not recorded`.
3. Links as §Links; `part-of` the direction only when a ledger node (todo
   after retire) names the structure by title or handle, else none;
   `derived-from` from the structure's own edges; the 785 `cites` edges.
4. `meta.converted_from=<dossier draft id>`, `converted_at`; a re-run skips
   a structure whose card exists (`card_structure`), appends only
   sources missing from `card_sources`.
5. Stamp `meta.cards_extracted_at` on the quest; the retire tool's
   `--apply` refuses a quest whose dossier narrative is non-empty and
   lacks it (same rule as `key_ideas_extracted_at`).

The dry run prints per quest: candidates, cards to create / to extend,
per-section line counts, sources unresolved (a handle that resolves to no
live structure), sections `not recorded` by name; then every card in full
for the first 10 candidates by id plus any Reto names (`--show st549017
st478029`), so he reads real cards, not counts. Pass 3 does not depend on
the retire apply; a card converted before retire gets its `part-of` from
`--rehome`.

### Pathway page (pass 4)

Today `precis_web/routes/refs.py::_pathway_provenance` builds the strip's
`eye_url` = `/eye/st<id>` of the candidate (gr549580: the fisheye replaced
the dossier link; `/eye/pw<id>` when there is no candidate), rendered by
`templates/refs/pathway_detail.html.j2`'s provenance strip next to
candidate · quest · logbook · run jobs; pinned by
`tests/precis_web/test_pathway_detail.py::test_pathway_detail_provenance_strip_branch_sections_and_annotations`
(asserts `/eye/st166700`, `fisheye`, no `dossier`). Once cards exist the
strip gains `card` → `/refs/memory/<id>` (the generic refs page;
`_DEFAULT_REFS_KINDS` lists memory), resolved as the candidate's
`related-to` memory tagged `card:experiment`, mouseover "why · expected ·
result · next"; the fisheye link stays (the card's own ring lists the
pathway, Reto's "links back to the pathway view"), and the one-`/eye/`-
anchor invariant holds because the card link is not an `/eye/` URL. No
card → no change in the strip. The structure detail page gets the same
item in the same slice. Test: the strip test gains a card fixture and
asserts the `/refs/memory/` link and its absence without a card.

## Quest navigation for the LLM

Target: an agent reaches any direction of a quest, and the evidence under
it, in ≤ 3 calls with bounded tokens, through the fisheye
(`utils/eye_render.py::render_eye`). No narrative renderer.

**Fisheye today on a migrated quest** (`render_eye` → `_render_note_eye` →
`_render_first_hop`, grouped by `refeye.RING_GROUPS`): direction roots read
as `contains` under Parts, 8 per label (`_NEIGHBOR_GROUP_CAP`) then
`… +N more` with a `q='contains'` expansion (≤ `_FIRST_HOP_EXPAND_CAP`
= 500); `_neighbor_label` shows no todo STATUS; children nested by
`parent_id` are not rendered, so a strand's directions are unreachable.
`ring=` (`RingFilter`) filters by kind/space only.

**Prod shape** (ledger nodes by `parent_chunk_id`, 2026-10-24):

| quest | roots | strands | nodes | live roots (childless) | children per strand |
|---|---|---|---|---|---|
| qu202467 | 200 | 75 | 442 | 118 (85) | avg 2.0, max 13, 3 over 8 |
| qu164903 | 53 live (225 counted retired chunks too, see Spike 1) | 40 | 249 live | 13 (5 open/active) | avg 2.5, max 9 |
| qu202468 | 85 | 30 | 223 | 42 (36) | — |

qu164903 reconciled by slice 1 (§Spike 1 results): the 225 counted 172
soft-retired nodes parented on a retired chunk; live roots are 53 (the
census's 51 predates two adds). qu164903 never needs `nest` or clustering.

**Cost per path** on qu202467. One neighbour line ≈ 30 tokens (handle,
≤ 80-char title, label, status); fisheye header (quest text + other groups)
≈ 1,000; a strand focus ≈ 150 + 30 × children (≤ 540).

| Path to one direction | calls | tokens, worst case | failure mode |
|---|---|---|---|
| today | 2 (quest, `q='contains'`) | ~7,000 | 242 non-root directions unreachable |
| (a) parameterised fisheye: every live root listed, strands first, terminal roots as count lines | 2 live (quest, strand); 3 terminal | ~5,100 live; ~7,500 terminal | 3.5k list paid on every quest open |
| (b) index nodes, fanout 12 (17 nodes) | 3 (quest, index, strand) | ~2,600 | index label is a summary the agent guesses from; a mis-grouped direction is hidden |
| scoped search (`under=` quest) | 2 (search, strand) | ~850 | needs a query that ranks the target in the top hits; default hybrid (lexical + semantic) so a paraphrase also lands, `mode='lexical'`/`'verbatim'` for exact terms |
| (c) balanced TOC on the overflowing ring (Reto 2026-10-24; §Balanced TOC) | 3.0 to 3.6 mean | ~2,100 | computed, no minted nodes; labels are keywords, so a miss on a label costs one backtrack call |

**Decision (revised after slice 1): (c) balanced TOC on the overflowing ring
+ scoped search; no synthetic index nodes.** Strand todos stay the only
authored structure. Measured on qu202467's real titles (§Spike 1 results),
(c) reaches any direction in 3.0 to 3.6 calls and ~2.1k tokens on average,
(a) in 2.1 calls and ~6.2k (first call alone ~4.6k), and (b) is dropped (a
node class, a minter, re-grouping on every tick, labels an agent has to
guess; (c) gives the same index computed on read). The loosened-cap list (a)
is folded into (c): a ring of ≤ k renders flat as today, a larger one
renders as clusters. Reto approved, 2026-10-24, the tick `nest` op opening
umbrella strand todos above 40 live childless roots (move a direction under
an existing strand, or open an umbrella strand todo with ≥ 3 members; the
umbrella is a direction the model authored, `part-of` the quest, inside the
rotation fence). With (c) navigation cost no longer depends on the root
count, so `nest` is tick hygiene (keeps the prompt's open list short), not
a navigation requirement; the 40 threshold stays as approved.

**Navigation layer** (slice 3; rendering only, `utils/eye_render.py`,
`utils/refeye.py`, `handlers/quest.py`, `handlers/todo.py`):

1. Quest and strand foci list their directions with the memory-hub branch
   (`_HUB_MEMBER_LABEL`, `_HUB_LIST_CAP`, today keyed on memory
   `section:index`), extended to "quest with `part-of` todos" and "todo with
   `parent_id` children".
2. A ring of ≤ k members (k default 8, a parameter) renders flat: strands
   before leaves; within each, `active`, `open`, newest `updated_at` first.
   A larger ring renders through §Balanced TOC; there is no ordering
   constraint on clusters. Terminal directions stay reachable by
   `q='contains:tried'`, which lists exactly those members (clustered when
   > k).
3. `_neighbor_label` prefixes every todo with its model-side status
   (`[active]`, `[tried]`, …) and a strand with its child count (`+13`), on
   every fisheye, not only quests.
4. A todo focus renders its `parent_id` parent and children.
5. `search(kind='todo', q=…, under='<handle>')` scopes to the item's
   subtree, as memory `under=` scopes to a hub (§`under=` semantics).
6. After step 6, evidence sits on directions; a direction focus shows it
   under Roadmap `served-by`, ordered by the existing
   `RING_GROUP_KIND_RANK`.

`under=` semantics, `ring` filter and the TOC follow below.

`view='directions'` returns the bounded open + do-not-repropose lists the
tick prompt reads (12 000-char budget, `dossier._OPEN_LEDGER_BUDGET_CHARS_DEFAULT`
moved to `directions`, `+N more` lines).

**Pinned by** `tests/test_quest_navigation.py` on a fixture shaped like
qu202467 (200 roots, 75 strands, 118 live): the quest fisheye renders the
200-root ring as a balanced cluster tree (§Balanced TOC) with every cluster
page ≤ 2,500 tokens (chars / 4); every root is reachable and appears once;
`q='contains:tried'` lists exactly the tried roots; a strand focus lists its
`parent_id` children; `under=` search returns only subtree members. Existing
memory-hub fisheye tests stay green unchanged.

### `under=` semantics

Follows the house convention (`term-taxonomy.md` `/x/**` = `under=x`,
`/x/*` = `under=x, depth=1`; `knowledge-mesh.md`; the taxon kind's
`under=`/`depth=`): `under=<any item id>` takes a quest, a strand todo or a
direction todo and returns the whole subtree below it, never the item
itself. `depth=N` limits it; `depth=1` is the direct children. Reto's
question, "is things directly linked, or can i say under=<id> hops=2?",
maps to `depth=2` (children and grandchildren); the parameter stays
`depth=`, not `hops=`.

Which edges count as "under" (the subtree of node X):

- child todos by `parent_id` (depth +1 per level);
- todos `part-of` a quest (depth 1 under the quest), then their
  `parent_id` descendants;
- evidence that `serves` a node in the subtree (finding, measure, paper,
  structure, draft, and a memory tagged `card:experiment`): included
  **by kind, not by flag**. `search(kind='todo',
  under=X)` returns directions only; `search(kind='finding'|'paper'|'measure',
  under=X)` returns the evidence that serves X or any todo in its subtree
  (the set `directions.quest_evidence` defines, slice 5), at depth =
  serving node's depth + 1 (so `depth=1` under a quest is its direct
  `serves` evidence). Omitting `kind` mixes both. A list `under=[a, b]`
  intersects, as on taxon.
- not counted: `related-to`, `cites`, and hypothesis edges
  (`supports`/`disputes`/`tests`), which are claims about a node, not
  membership.

The search ranks in the handler's default hybrid mode (lexical + semantic)
and honours `mode=` (`lexical`, `semantic`, `verbatim`).

### Ring filter `q=`

`RingFilter` (`utils/eye_render.py`) gains `q`: `args={'ring': {'q':
'urea coupling'}}` keeps the ring members matching the query (the handler's
hybrid match, restricted to the ring's ids; `mode=` honoured), before
clustering. Evaluated: worth adding, cheap, no new index; it makes the
filter work on any focus (a paper's neighbours, a finding's evidence), where
`under=` only exists for tree kinds. Composes with the TOC: filter, then
cluster what is left. Slice 3b.

### Balanced TOC on the overflowing ring

Reto 2026-10-24: "I really like toc on fisheye, we don't even need the
order, we can just make the best clusters (most even, shannon stuff), and
then drill for details ... The 8 for toc was arbitrary, it can be a param
but 8^3 is very large ... If we can well-balance the TOC tree (we are
allowed to!) then it's super awesome."

- **Applies** to any ring group (or the `under=` subtree) with more than k
  members, where today the cap prints `… +N more` (`_NEIGHBOR_GROUP_CAP`).
  A group of ≤ k renders flat with no cluster level.
- **Balance is the primary objective**, at every level: balanced k-way
  splits (size-constrained k-means, capacity `ceil(n/k)` per cluster),
  applied recursively. A minimum cohesion floor keeps labels meaningful (a
  split whose mean centroid cosine is below the floor falls back to the
  unconstrained split for that node). Depth is derived from N
  (`ceil(log_k N)`), never a fixed level count. k defaults to 8 and is a
  parameter (`args={'toc_k': 6}`).
- **Alternative mode** `toc='specific'`: cohesion first (average-linkage
  dendrogram cut top-down by splitting the largest cluster until k
  children). Measured below; not the default.
- **Labels** are per-cluster keywords (top terms of the cluster minus its
  parent's), with a count and a drill handle; leaves render as normal
  fisheye lines (handle, status, title). Keyword extraction reuses
  `utils/rake.py` and `utils/semantic_keywords.py` (what `view='toc'` uses
  for its segment labels; `utils/toc.py` segments one ref's chunks in
  reading order, so only its labelling is reused, not its contiguous
  segmentation; `utils/cluster_map.py` has the batch SOM and
  `ctfidf_words` for sibling-scoped labels).
- **Features**: chunk or title embeddings when present. New refs may have
  none (gr513749 embedding backlog), so a lexical fallback is specified:
  TF-IDF over title tokens (stopwords out, plural stripped), used for the
  whole ring when more than 10% of members lack an embedding and per
  member otherwise. The spike measured the lexical path only (no embedder
  in the spike), so the numbers below are the fallback's; embeddings should
  only improve label hit-rate.
- **Computed on read, never minted.** Cached per `(focus, ring filter, k,
  mode)` and invalidated when the neighbourhood fingerprint (member ids +
  statuses + max `updated_at`) changes. Deterministic seed, so the same
  fingerprint gives the same tree and drill handles stay valid.

### Spike 1 results (2026-10-24)

Mint: prod ledger chunks of qu202467 (442 live nodes, 200 roots) and
qu164903 (249 live nodes, 53 roots) read with `scripts/prod-psql --ro` and
minted as todos (`parent_id`, `part-of` quest, `STATUS` by the mapping
table) on the dev DB; `get(kind='quest', extent='fisheye+1hop')` rendered
in-process. The cluster numbers are an offline model over the same real
titles (lexical features, k = 8, a 35-token cluster line, 30-token leaf
line, 1,000-token first call, 150 per later call; target = each of the 442
nodes, plus 10 random), with an oracle that always picks the right cluster.
Label hit-rate is the share of path clusters whose label shares a stem with
the target's title.

| Path (qu202467, N = 442, ring = 200 roots) | calls (mean) | tokens (mean / max) | shape |
|---|---|---|---|
| today (flat, 8 + `+192 more`) | 2, 242 unreachable | 185 first call | measured |
| (a) loosened: all live roots listed | 2.09 | 6,153 / 7,738 (first call 4,588) | 118 live, 82 terminal |
| (c) balanced TOC, roots ring | 3.55 | 2,119 / 2,520 | 2 levels, L0 8 × 25, CV 0.00; L1 64 × 3.1, CV 0.33 |
| (c) balanced TOC, whole subtree (`under=`) | 3.00 | 2,067 / 2,070 | 2 levels, L0 8 × 55, CV 0.02; L1 64 × 6.9, CV 0.06 |
| specific TOC, roots ring | 3.62 | 2,156 / 2,590 | 3 levels, L0 CV 0.28, L1 CV 0.62, sizes 1..10 |
| specific TOC, whole subtree | 3.66 | 2,241 / 2,440 | 3 levels, L0 sizes 6..115, CV 0.57 |
| `under=` search, 10 hits | 1 (+1 on a miss) | ~500 per call | hit@10 1.00 with the target's 3 rarest words (oracle, optimistic) |

Label hit-rate: balanced 0.86 (roots) / 0.76 (subtree); specific 0.86 /
0.87. Fanout: k = 12 gives 2 levels and 2,339 tokens at 3.55 calls (balanced
roots), k = 5 gives 3 levels, 4.55 calls, 2,180 tokens. For the 10 random
targets, balanced roots took [3,4,3,4,4,3,4,4,4,4] calls at 2,127 tokens
mean; loosened (a) [2,2,1,3,2,1,3,3,2,3] at 6,058.

Reading: balanced and specific TOC cost the same tokens (~2.1k) and calls
(~3.6); balanced has even sizes (CV 0.00 to 0.33 against 0.28 to 0.73), a
2-level tree where specific needs 3, and nearly the same label quality, so
balanced is the default and specific an option. TOC beats (a) on tokens by
~3x for ~1.5 more calls and does not grow with the root count; `under=`
search is the cheapest path when the agent has a term, TOC when it browses.
They complement; neither replaces the other. Hub cap: the 300-row cap is
replaced by the clustering bound `_FIRST_HOP_EXPAND_CAP` = 500 (a ring over
500 clusters its first 500 by recency and says so). `nest` threshold 40:
see §Decision.

**qu164903 root-count reconciliation.** Chunks with `parent_chunk_id` =
the ledger container: 53 live roots (40 strands, 13 childless, 5 of those
open/active). The 225 in the Prod-shape table was every node whose parent
is the container plus the 172 soft-retired nodes parented on a retired
chunk (53 + 172 = 225); 296 of 545 nodes are retired in all. The census's
51 predates two adds; one live root carries `meta.parent_unresolved`. So
the earlier "145 live childless roots" for qu164903 was wrong: it needs
neither clustering nor `nest`, and slice 5's apply must skip retired node
chunks (`_load_ledger_nodes` already does, via `reading_order`).

**Relation pin.** `test_part_of_todo_to_quest_passes_constraints` is in
`tests/test_relation_constraints.py` and passes: todo → quest `part-of` is
accepted; quest → todo `part-of` afterwards is refused as a cycle. No
constraint migration needed before slice 5.

## Export

No quest export render (§Decisions Q5). An export is a writer-authored
draft that `serves` the quest, exported by the existing draft export
(`export/docx.py::export_docx`, PDF `draft_export` job); no export code
reads the dossier (`grep -n dossier src/precis/export` is empty).
`quest-artifacts-in-dossier.md` retargets to the writer pipeline
(`paper-writing-pipeline.md`).

- **Nanobuds (dr173020).** A hand-authored draft: `draft-of` td173019,
  `serves` qu459585, no `dossier-of`/`paper-of` edge. Its export never
  touches the dossier. The one coupling is `draft_refresh`: a refresh of any
  draft that `serves` a quest calls `dossier.add_attempt`/`mark_attempt` on
  that quest (`workers/job_types/draft_refresh.py::_dispatch`), which also
  re-creates a dossier via `ensure_dossier` if none exists. Slice 4's
  refusal stops the re-creation; slice 8 moves the calls to logbook entries.
  Drafts are not evidence for step 6: dr173020 keeps its `serves` qu459585.
- **Proof** (new tests): `test_serving_draft_untouched` in
  `tests/test_retire_dossier.py` (fixture: quest + dossier + a draft
  `serves` the quest with `[dc]` xrefs and cites; run the migration; the
  draft's chunks, links and `export_docx` bytes are equal before and
  after); `test_refresh_on_retired_quest_logs_and_mints_no_dossier` in
  `tests/workers/test_draft_refresh_job.py`; the existing
  `tests/test_docx_handle_repro.py`, `tests/test_export_provenance_strip.py`,
  `tests/test_export_async.py` stay green unchanged. The dry run lists
  dr173020 under "untouched (serves)" for qu459585.

## Migration of existing dossiers

Prod census (2026-10-24, `scripts/prod-psql --ro`): 25 live dossiers, all
quest-owned: 19 live quests (3 `active` — qu164903, qu500066, qu509651;
the rest `dormant`/`abandoned`; bodies: 16 materials, 2 inquiry —
qu459585, qu500066 — 1 roadmap, 0 weave) and 6 retired quests. 1376 ledger
nodes on 15 quests (open 149, active 374, tried 468, ruled-out 385). Roots
per quest up to 200 (qu202467), 85, 80, 51 (qu164903). Node text ≤ 670
chars. Narrative 99 chunks, 35–1389 words per quest, outlier qu175733
(abandoned) 9879 words. 667 `frontier-tree` chunks, 11 `dialectic`
containers (entries on qu164903 only), 1 `capability-ledger` (dossier
458879, roadmap root qu453863). `paper-of`: 0. Inbound links to dossier
drafts: 35 `related-to` (morning briefs, memories); outbound: 1 `cites`.

**Schema:** none. Todo `STATUS` values and `part-of` exist; no axis change.
The last slice adds one forward-only migration that sets
`domain_kinds = '{}'` on `dossier-of`, `has-dossier`, `paper-of`, `has-paper`
and rewrites their `description` to name `part-of`/`serves`, so the link
doors (`utils/link_tag_ops.py::check_relation_constraints`) refuse new edges
with that text as the reason; the rows stay. The same migration rewrites the
`part-of`/`contains` descriptions kind-neutral. Internal `create_draft(...,
relation=)` writes bypass the doors, so the code guard is slice 4's
`ensure_dossier` refusal. Number assigned by builder-ce at land time.

**Step 0 — key ideas into the graph** (§Decisions Q4/Q6/Q7; a pre-pass, not
in the retire transaction): `precis quest extract-key-ideas <qid>|--all
[--apply]` (`src/precis/quest/key_ideas.py`), dry run by default, all 25
dossiers including the 6 of retired quests (Q6: "the information stays
relevant"). One LLM call per dossier over its narrative, dialectic
hypotheses and newest `frontier-tree` chunk returns claims, each with a
verbatim supporting span from the dossier and the `[pa…]`/`[fi…]` cites the
dossier gives for it; a claim whose span is not found verbatim is dropped
and listed. Each claim matches an existing finding by the taproot restated-
hub check (`taproot/hub.py::_find_restated_hub`) or mints a finding
(hypothesis `claim_type` when the span hedges), with `cites` edges to the
cited refs, `serves` the quest, `meta.from_dossier_id` + `meta.claim_sha`.
Step 6 re-homes it to a direction like any quest evidence. A re-run skips a
claim whose `(from_dossier_id, claim_sha)` exists. Stamps
`meta.key_ideas_extracted_at` on the quest; the retire tool's `--apply`
refuses a quest whose dossier has a non-empty narrative and no stamp. The
dry run prints per dossier: claims, dedupe hits, dropped (no verbatim
span), cites resolved/unresolved.

**Tool:** `precis quest retire-dossier <qid>|--all [--apply]`
(`src/precis/quest/retire.py`), dry run by default. Per quest, one
transaction:

1. Ledger forest (`dossier._load_ledger_nodes` on the existing dossier id —
   not `read_ledger_nodes`, which goes through `ensure_ledger_chunk` →
   `ensure_dossier`) → todos in DFS order: roots `part-of` the quest,
   children by `parent_id`; status by the mapping table;
   `meta.from_ledger_chunk=<handle>` makes a re-run skip a node whose live
   todo exists. `parent_unresolved` survives as `meta.parent_unresolved`.
   Depth > 10 (the todo wall) attaches at depth 10 with
   `meta.flattened_from_depth`.
2. Narrative → one logbook entry (`entry_type='note'`, `by=system`), full
   text, `meta.archived_narrative=true`, `meta.retired_dossier_id`.
   `tick._logbook_tail` and the view skip archived entries, so qu175733's
   9879 words never reach a prompt. A re-run finds the entry by
   `meta.retired_dossier_id` and writes none.
3. Dialectic entries: support/counter already exist as edges; each
   `experiment` entry → a direction todo (`open`) under its hypothesis's
   strand, with `meta.hypothesis_id=<finding id>` and
   `meta.from_dialectic_entry=<handle>`.
4. `frontier-tree`, `capability-ledger`, seed, ledger and dialectic chunks:
   left on the retired draft. Nothing reads a retired dossier
   (`dossier_ref_id` excludes `retired_at`), and keeping them makes
   `--undo` lossless.
5. Dossier draft: soft-retired (`retired_at`), `dossier-of` link rows
   deleted, the 35 inbound `related-to` rows kept (they resolve to a
   retired ref, like any retired ref). Hard deletes of the draft: not done.
6. **Re-home evidence.** For every live finding, measure, paper and
   experiment card (memory tagged `card:experiment`) with a `serves` edge
   to the quest, pick a home among the quest's direction and strand todos
   by the first rule that yields one (a card's home is written as
   `part-of` the todo, its `serves` quest edge stays — §Links):
   - a. *Hypothesis edge.* The evidence has a `supports`, `disputes`,
     `contradicts` or `tests` edge to a hypothesis finding H, and H has
     experiment direction todos (`meta.hypothesis_id=H`, step 3): home = the
     narrowest todo containing all of them (the one todo, or their lowest
     common ancestor). H itself homes by the same rule. A card homes
     through its pathways' `tests` edges.
   - b. *Text match.* `directions._match_nodes(forest, <evidence title>)`:
     exact match, then the ≥ 40-char unique-prefix pass; for a card the
     matched text is its structure's title and handle.
   - c. *Paper via its findings.* A paper with `establishes`/`corroborates`
     edges to findings homed by a or b: home = the narrowest todo containing
     all those homes.

   Several hits on one root-to-leaf path → the deepest. Hits in disjoint
   branches, a common ancestor that is the quest itself, or no hit → the
   quest edge stays and the evidence is listed "unplaced" in the dry run
   (with the candidate todos when ambiguous). A move adds `serves` → the
   todo with link meta `{rehomed_from: <qid>}`, then removes `serves` → the
   quest; `--undo` reverses moves by that meta. Before any edge moves, every
   reader of a quest's `serves` set (`quest/gaps.py`, `search.py`,
   `staircase.py`, `reweight.py`, `compute.py`, `roadmap_tick.py`,
   `roadmap_ledger.py`, `handlers/quest.py`, `precis_web/routes/refs.py`)
   reads it through one new `directions.quest_evidence(store, qid)` —
   `serves` the quest or any todo in its direction tree — so a move changes
   no count; slice 5 lands that switch with the tool. `--rehome <qid>` runs
   this step alone on an already-retired quest, so evidence the tick links
   to the quest after cutover can be re-homed the same way; making the tick
   link narrowly at write time is out of scope here.
7. Stamp `meta.dossier_retired=true` and `meta.retired_dossier_id=<draft
   id>` on the quest — the cutover flag and the undo pointer.

The dry run prints per quest: nodes by status → todos, roots/strands, max
depth and flattened count, narrative words → archive entry, dialectic
entries → todos, chunks left on the retired draft by family, links removed,
inbound links kept, drafts that `serve` the quest (untouched), and evidence
re-homed by rule (a/b/c, per kind), ambiguous (with candidates) and
unplaced. Totals at the end. Reto reads the `--all` dry run before any
`--apply`.

**Rollback.** `precis quest retire-dossier --undo <qid>`, one transaction,
refused under the same in-flight check as `--apply`: clear `retired_at` on
the draft named by `meta.retired_dossier_id`, re-insert its `dossier-of`
link, soft-retire the todos carrying `meta.from_ledger_chunk` or
`meta.from_dialectic_entry`, move each `{rehomed_from: <qid>}` edge back to
the quest, clear `meta.dossier_retired` and `meta.retired_dossier_id`. The
archived-narrative entry stays (WORM); the view skips it. Step-0 findings
stay (they are graph content, not dossier state). Since step 4 deletes no
chunk, the dossier comes back as it was at apply time. Direction edits made
between apply and undo (status changes, new directions) are not written back
to the ledger; the undo report lists them. Chosen over keeping the link rows
until slice 10: `dossier_ref_id` ignores a retired draft, so live link rows
alone restore nothing, and the minted todos would still need retiring, so an
undo has to exist either way; this makes it one tested command. Test:
`test_apply_undo_apply_round_trip` — after apply → undo, the dossier's
ledger forest, chunk set, links and quest meta equal the pre-apply snapshot
and no minted todo is live; the second apply yields the same todo tree (by
`meta.from_ledger_chunk`), the same re-homed edges and still one archive
entry.

**Mid-flight quests.** There is no per-quest tick lock. A quest's loop is
one perpetual `quest_tick` coordinator job (`params.quest_id`); between
ticks its `meta.coordinator_state.phase` is `await`, and while a sliced
tick is parked mid-flight it is `tick:<stage>` with the tick's checkpoint
in `coordinator_state.tick` (`workers/job_types/quest_tick.py::_phase_tick`).
Two guards:

1. `--apply` and `--undo` refuse a quest whose `quest_tick` job is
   `STATUS:running` or whose `coordinator_state.phase` starts with `tick:`:
   "tick in flight (phase tick:apply); retry once it reaches await". The
   check is advisory: a slice can be claimed right after it, and the CLI's
   unsliced `precis quest tick` has no job at all.
2. The tick re-reads `meta.dossier_retired` at the start of its `apply` and
   `finish` stages (`tick.py::_stage_apply`, `_stage_finish`), not only at
   tick start. A tick that built its prompt on the old path and resumes
   after the flip routes its `ledger_ops` through `directions` (same op
   shape, mapped at the boundary) and its dialectic experiment entries to
   direction todos as in step 3, skips `update_frontier_tree`, and writes
   its `dossier_text` as the synthesis entry instead of calling
   `rewrite_dossier`. A flip that commits inside a running stage, after its
   re-read, hits `ensure_dossier`'s refusal (slice 4): the write is logged
   and dropped, never landed in a dossier.

Each quest flips individually; ticks between quests run the old or new path
by the flag.

## Module fates

Grep of `src/` + `scripts/` (non-pycache): 73 files mention "dossier", 49
of them Python. Every file is in exactly one group below.

**New:** `quest/directions.py` (slice 2), `quest/cards.py` (slice 2b:
`ensure_card`/`append_line`/`set_outcome`, the `append_entry` hook, the
`extract-cards` conversion), `quest/key_ideas.py` (slice 6),
`quest/retire.py` (slice 5).

**Deleted (last slice):** `quest/dossier.py` storage — `ensure_dossier`,
`ensure_ledger_chunk`, `rewrite_dossier`, `read_dossier`, `read_narrative`,
`read_ledger`, the ledger chunk/tag writers, legacy-ledger migration,
`update_frontier_tree`, the dialectic block storage, `paper_ref_id`,
`dossier_ref_id`. The text logic moves to `quest/directions.py` first
(slice 2): `_match_nodes`, `_find_near_dup_node`, `_elements_conflict`,
`_is_near_dup`, `_sig_tokens`, `_do_not_repropose_lines`, the open-list
budget. `cli/quest.py::_cmd_dossier`/`_cmd_dossier_dedup` (replaced by
`retire-dossier` and `view='directions'`). `store/types.py`'s `ATTEMPT`
axis entry and the `dossier-of`/`paper-of` relation-registry rows in Python.

**Deleted (slice 7, weave):** `quest/weave.py`, `weave_tick.py`,
`placement.py`, `claims.py`, `residual_cluster.py`, `weave_review.py`,
`citation_mint.py` (1909 lines) and `cli/quest.py`'s weave command. Their
importers first: `QUEST_BODY_*` constants move from `weave_tick.py` to
`quest/body.py` (read by `body.py`, `cli/quest.py`, `handlers/quest.py`,
`workers/job_types/quest_tick.py`); slice 7 greps the remaining importers
(`review_guard.py`, `review_fanout.py`, `gaps.py`, `roadmap_tick.py`,
`utils/ref_tree.py`, `todo/guards.py`, `utils/llm/router.py`,
`utils/prompt/predicates.py`, `taproot/backfill.py`,
`workers/planner_prompt.py`) and cuts each weave branch.

**Rewritten onto edges/views:**

| Module | Today | After |
|---|---|---|
| `quest/tick.py` | `build_tick_prompt` reads narrative, ledger forest, dialectic; `_apply_narrative_gate`; apply stage runs `ledger_ops`→`add_attempt`/`mark_attempt`, `append_ledger_entry`, `dialectic_ops`; finish stage `update_frontier_tree`, `rewrite_dossier`; the ladder (`_stage_ladder` → `_commit_rung`) materialises re-prompt proposals with no hypothesis entry | prompt reads the newest synthesis entry + `directions` open/do-not-repropose lists; `direction_ops` (add/mark by `td` handle or quoted text; `nest` when > 40 live childless roots) → `directions`; `dossier_text` replaced by one synthesis entry (Q1); finish drops both dossier writes; apply and finish re-read the flag; proposals carry `expected` + `direction`, apply stashes `proposal_entries`, the ladder logs its rung's proposals as `hypothesis` entries first (slice 2b) |
| `quest/compute.py` | `_ensure_candidate_detail` mints the structure, `serves` quest, `derived-from` parent; `harvest_measures` writes result entries and `related-to` pathway | plus the card ops at each of those points (§Writers); `run_compute_step(entry_ords=)` |
| `quest/logbook.py` | `append_entry` inserts the chunk | plus the card hook: `cites` edge + card line per structure handle in the text (§Logbook edges) |
| `workers/job_types/quest_tick.py` | `outcome.dossier_rewritten` counts a tick as substantive (dry-tick/empty-punt detection, `_phase_tick`); routes weave-body quests to `weave_tick` | substantive = a direction op applied or a synthesis entry written (`TickOutcome.directions_changed`); weave route removed (slice 7) |
| `workers/hub_refine.py` | stage 5 widen seeds from hub due-sets | plus a quest seed: papers in `directions.quest_evidence` with no inbound evidence edge (`establishes`/`corroborates`/`supports`/`disputes` from or to a finding) → the existing canon/judge path → `taproot/hub.py::attach_evidence`/`apply_placement`; bounded per pass (slice 7) |
| `quest/rulings.py` | `dossier_ref_id` + dialectic experiment entries → measurement rulings | experiment direction todos (`meta.hypothesis_id`) + hypothesis findings; `tests` edge unchanged |
| `quest/roadmap_tick.py` | `ensure_dossier`, regenerates `capability-ledger` chunk | quest view/page renders `roadmap_ledger` output on read |
| `quest/roadmap_ledger.py` | docstring: pins into the root's dossier | render target = quest view (pure module, prose only) |
| `quest/frontier.py` | tree rendered into the dossier chunk | `render_frontier_tree` called by the quest page and `view='frontier'` |
| `quest/body.py` | imports `QUEST_BODY_*` from `weave_tick` | owns them; `weave` body value dropped (0 quests) |
| `quest/compute.py` | `reset_compute` stubs the narrative unless `keep_dossier` | logbook entry only; `keep_dossier` parameter removed |
| `quest/search.py` | excludes the quest's `dossier-of`/`paper-of` drafts from hits | exclusion removed with the relation rows (slice 10) |
| `quest/review_guard.py`, `review_fanout.py` | machine-owned = `dossier-of`/`paper-of` source; weave review branches | machine-owned check removed (slice 10; no live machine-owned draft after retire); weave branches removed (slice 7) |
| `handlers/draft.py` | `_MACHINE_OWNED_RELATIONS` refusal | removed in slice 10 |
| `handlers/quest.py` | `view='dossier'` (`_render_dossier`), fisheye text | `view='directions'`, `view='frontier'`, fisheye per §Quest navigation; `view='dossier'` answers with `view='directions'` plus a one-line rename note until the skills ship |
| `handlers/todo.py` | todo fisheye without parent/children | `parent_id` parent + children, `under=` search (slice 3) |
| `utils/eye_render.py`, `utils/refeye.py` | hub listing only for memory `section:index`; no todo status in labels; 8-per-label cap; `RING_GROUP_KIND_RANK` ranks Roadmap only; `UP_RELATIONS['memory']` = `part-of` | §Quest navigation items 1–4; `"Notes & links": ("memory",)` so a card lists before pathways; memory's up rule gains `("serves", "out")` for a card with no direction yet (slice 2b) |
| `workers/job_types/draft_refresh.py` | attempt label into the owning quest's ledger | logbook `observation` entry; no direction (an LLM failure is not a ruled-out direction) |
| `precis_web/routes/refs.py` | quest page: neighbourhood block (gr541346 removed the dossier block), `paper_ref_id`; pathway page `_pathway_provenance` links the candidate's fisheye (gr549580) | direction tree (fisheye order), newest synthesis entry, frontier tree; pathway and structure pages add the `card` item (§Pathway page, slice 8) |
| `templates/refs/quest_detail.html.j2`, `pathway_detail.html.j2` | quest neighbourhood; pathway provenance strip (candidate · quest · logbook · fisheye · jobs) | Directions + Synthesis + Frontier tree sections; strip + `card` |
| `precis_web/routes/smartdraft.py` | `dossier-of` in link sort order | entry removed |
| `handlers/_integration_view.py`, `store/_integration_ops.py` | integration ledger over "a topic dossier" draft | any draft (prose + one lookup) |
| `quest/narrative_budget.py` | `meta.dossier.*` overrides | `meta.narrative.*`, old key fallback; `narrative_growth_gate` gates the synthesis entry and `draft_refresh` |

The `serves`-set readers named in step 6 also switch to
`directions.quest_evidence` (slice 5).

**Prose only** (docstring/comment/prompt wording): `quest/__init__.py`
(package invariant "The dossier can't bloat…" rewritten for directions +
synthesis entry), `quest/cascade.py`, `taproot/reground.py`,
`workers/classify.py`, `classify_topics.py`, `axis_pass.py`, `registry.py`,
`cli/classify.py`, `cli/_worker_passes.py`, `store/protocols.py`,
`_chunks_ops.py`, `_tag_filter.py`, `_draft_review_ops.py`,
`handlers/finding.py`, `precis_web/linkify.py`, `claim_render.py`,
`routes/claim.py`, `templates/claim/_body.html.j2`,
`precis_pathway/toon_views.py`, `precis_estimate/__init__.py`,
`utils/llm/json_reply.py`, `utils/units.py`,
`data/topics/co2-conversion.yaml`, `data/axes/README.md`,
`scripts/context-audit/capture.py`, `CATALOG.md`.

**Untouched:** sealed migrations `0065`, `0067`, `0085`, `0089`, `0095`,
`0142`, `0180`, `baseline/schema.sql`.

**Tests** (47 files mention it): deleted with the storage —
`test_dossier_hygiene.py`, the dossier half of `test_quest_dialectic.py`;
deleted with weave (Q8, feature removed, no replacement) — `test_weave*.py`,
`test_placement.py`; rewritten — `test_quest_tick.py` (193 hits),
`test_quest_compute.py`, `test_quest_rulings.py`, `test_quest_roadmap.py`,
`test_quest_tick_job.py`, `test_quest_loop_fixes.py`,
`test_quest_cascade.py`, `test_quest_gaps.py`, `test_integration_ledger.py`,
`test_review_fanout_writeback.py`, `test_draft_handler.py`,
`precis_web/test_quest_dashboard.py`, `test_pathway_detail.py`,
`test_draft_block_routes_handler_path.py`, `workers/test_draft_refresh_job.py`,
`test_relation_constraints.py`; new — `test_quest_cards.py` (slice 2b),
a new memory-part-of-todo pin in test_relation_constraints.py,
the card fixture in `precis_web/test_pathway_detail.py` (slice 8); the
rest are incidental strings. A slice that
deletes a test moves its assertion into the replacement test in the same
commit (no loosening; Reto rules on any test that has no replacement other
than the weave tests above).

**Skills:** rewrite `precis-quest-help` (dossier, ledger, `view='dossier'`
sections → directions, navigation recipe: quest fisheye → strand →
evidence, `under=` search; the card recipe: structure fisheye → `card:`
memory → `~Why`/`~Result`/`~Next`), `precis-memory-help` and
`precis-pathway-help` (the `card:experiment` tag, system-owned `card_*`
meta, agents extend `Analysis`/`Next`), `precis-quest-writing-help`, `precis-relations`
(`dossier-of`, `paper-of`, new `part-of` on quests, the narrowest-`serves`
rule), `precis-roadmap-help`, `precis-fisheye-help` (status labels, hub
listing on quests and strands, `q='contains:<status>'`); one-line edits in
`precis-overview`, `precis-tags` (`ATTEMPT:`), `precis-draft-help`,
`precis-orcid-help`, `precis-paper-tag-axes`, `precis-estimate-help`;
manual `04-watching-the-loop.md`; glossary `dossier` → retired pointer,
`frontier tree`, `role`, `ratchet`, `weave` → retired pointer.

## Slices

Every slice lands green with the dossier path still working for quests that
are unflagged and have a dossier. Tier: Sonnet = coder agent or Devin from a
complete spec; Opus = design judgment inside the slice.

| # | Slice | Size | Tier | Acceptance |
|---|---|---|---|---|
| 2 | `quest/directions.py` (todo-backed list/add/mark/nest, near-dup upsert, do-not-repropose + open renders, both status maps, `quest_evidence`; text logic moved from `dossier.py`, which re-imports it); rotation fence — **done 2026-10-24** | M | Sonnet | `test_quest_directions.py` ports every `test_dossier_hygiene.py` assertion onto todos (Rh/Ru veto, prefix match ≥ 40 chars, never-regress merge, ancestor shadow) and pins the model-side map (`blocked`/`paused` on the open list); `test_todo_views` case: a direction todo is absent from `doable`/`queue`/nursery (the relation pin test already landed in slice 1) |
| 2b | Card writer (pass 2, after Reto rules on §Open for Reto): `quest/cards.py`; `_ensure_candidate_detail` creates/extends the card and back-links the hypothesis entry; `_stage_apply` stashes `proposal_entries`; the ladder logs rung proposals as `hypothesis` entries before `run_compute_step`; `harvest_measures`, `_stamp_preflight_dead_end`, `promote_tiers`, `redispatch_candidates`, `apply_reruns`, `graduate_frontier`, `apply_dialectic_op` write their lines; `append_entry` hook (`cites` edge + routed line per structure handle); proposal schema `expected` + `direction`; `RING_GROUP_KIND_RANK` + memory up rule | L | Opus | `tests/test_quest_cards.py`: a proposal tick creates one card whose `Why` quotes the hypothesis entry `[ql…]` and whose `Expected` is `not recorded` without `expected`; a ladder commit creates a card whose `Why` opens `not recorded — auto-committed …` and quotes the rung's entry; a second pathway on the same structure extends the same card (no second card); a duplicate proposal adds one `Why` line; `derived-from` parent → card `derived-from` + `Lineage`; a dialectic `predicts:` → `Expected`; harvest → `Result` lines, `related-to` pathway, `outcome:` tag, title recomputed; an observation naming `[st…]` → chunk-scoped `cites` + `Analysis` line, and a card with `Why: not recorded` when none existed; a re-run writes nothing (`card_sources`); `_validate_meta` refuses `card_*` from an agent; `direction_ref_ids` ignores the card; structure fisheye lists the card first under Notes & links; the writer passes `_machine_edge_admission` under the tick's write context; relation pin `test_part_of_memory_to_todo_passes_constraints` |
| 2c | Conversion (pass 3): `extract-cards` dry run / apply / `--show`, `cards_extracted_at` stamp, the 785-edge backfill | M | Sonnet | `test_quest_cards.py::test_extract_cards_*`: a fixture quest with a dossier, logbook and three candidates (one auto-committed, one with a dialectic `predicts:`, one with two pathways) converts to three cards whose every line is a substring of a source chunk or structure meta, `not recorded` where no source, idempotent re-run, dry run prints the counts and the `--show` cards; ops: qu164903 dry run to Reto before any apply |
| 3 | Navigation layer (§Quest navigation 1–6, minus clustering) and `view='directions'`/`'frontier'`; todo status labels, `parent_id` parent/children, `under=`/`depth=` search with the edge rules above | M | Opus | `tests/test_quest_navigation.py` as pinned above (flat rings); `under=` returns exactly the subtree, `depth=1` the direct children, evidence kinds include `serves` evidence and cards; memory-hub fisheye tests unchanged |
| 3b | Balanced TOC on fisheye overflow (§Balanced TOC): size-constrained k-means tree, keyword labels, lexical fallback, cache by neighbourhood fingerprint, `toc_k`, `toc='specific'`, ring `q=` filter; replaces the `… +N more` cap and the loosened-cap list | M | Opus | on the qu202467-shaped fixture: every root reachable exactly once, per-level size CV ≤ 0.35, ≤ 2 levels at k = 8, each page ≤ 2,500 tokens, same tree for the same fingerprint, lexical fallback with no embeddings, a ≤ k ring unchanged; the 3 pre-existing cap tests updated, not loosened |
| 4 | Tick on the flag: quests with `meta.dossier_retired` and quests with no dossier use directions + synthesis entry + `direction_ops` (incl. `nest`); apply and finish re-read the flag; `ensure_dossier` refuses (raises `DossierRetired`) when the quest is flagged **or has no dossier**, so from this slice on it returns existing dossiers and creates none; each pre-slice-8 caller (`roadmap_tick`, `draft_refresh` via `add_attempt`/`mark_attempt`; `rulings` only reads via `dossier_ref_id` and already no-ops without a dossier) catches the refusal and skips its dossier write with a log line | L | Opus | Synthesis entry = `entry_type='reflection'` + `meta.synthesis=true`, WORM, ≤ `target_words`, gated by `narrative_growth_gate` against the previous synthesis entry; archive marker = `entry_type='note'` + `meta.archived_narrative=true`; no `ENTRY_TYPES` change, because the vocabulary is listed to the model in the tick prompt and accepted by the handler's `entry=`, model entries carry no meta (`_stage_apply` calls `append_entry` without `extra_meta`) so a model `reflection` never reads as a synthesis, and an added `synthesis` type would invite writes that `clamp_entry_type` would otherwise turn into `note`. Tests in `test_quest_tick.py`: flagged fixture runs llm→finish with no `dossier-of` link created and one synthesis entry; the prompt picks the synthesis entry over a newer plain `reflection`; `test_prompt_context_sections_bounded` (synthesis + directions sections ≤ `target_words` words + 12 000 chars on a 500-direction fixture); `nest` moves a direction under a strand and opens an umbrella only with ≥ 3 members; `test_no_dossier_resurrection` — every `ensure_dossier` path (`update_frontier_tree`, `ensure_ledger_chunk` and its readers/writers, `rewrite_dossier`, `ensure_dialectic_chunk`, `roadmap_tick`, `draft_refresh`) on a flagged quest and on a dossier-less quest creates nothing, plus a tick checkpointed at `tick:apply` and one at `tick:finish` on an unflagged quest, flag flipped, resumed: no dossier write, its ledger ops present as todos, its `dossier_text` as the synthesis entry; unflagged-with-dossier fixtures unchanged |
| 5 | `retire-dossier` tool: dry run, apply (steps 1–7), `--undo`, `--rehome`, in-flight check, step-0 and cards stamp checks; `serves`-set readers switched to `directions.quest_evidence` | M | Sonnet | `test_retire_dossier.py`: lossless round trip (forest == todo tree, statuses by the table), idempotent re-run, archived entry excluded from `_logbook_tail`, `test_serving_draft_untouched`, `test_apply_undo_apply_round_trip`, re-home by rule a, b and c, deepest-on-path, disjoint hits stay on the quest and print as ambiguous, a card re-homes as `part-of` the todo with its `serves` kept, refuses while the job is `running` or at `tick:*`, refuses a quest with narrative and no `key_ideas_extracted_at` or no `cards_extracted_at`; reader tests: quest evidence counts equal before and after a re-home |
| 6 | Step 0: `extract-key-ideas` (prompt, verbatim-span check, restated-hub dedupe, cites, idempotency, stamp) | M | Sonnet build; Opus reviews the prompt on 3 real dossiers' dry runs | `test_key_ideas.py` with a stubbed LLM: a claim without a verbatim span is dropped and listed; a restated claim links the existing finding; cites resolve to `cites` edges; re-run writes nothing; stamp set |
| 7 | Weave delete + `hub_refine` quest seed: move `QUEST_BODY_*` to `body.py`, delete the seven modules, their CLI command, routes and tests; add the seed | M | Sonnet | `rtk proxy grep -rln "weave_tick\|residual_cluster\|citation_mint" src` empty; `tests/workers/test_hub_refine.py` case: a quest-serving paper with no inbound evidence edge is selected, one already placed is not, the pass bound holds |
| 8 | Remaining consumers: web quest page, rulings, roadmap capability ledger, `draft_refresh`, compute reset; pathway + structure page `card` item (pass 4, §Pathway page) | L (split a–e by module) | Sonnet | each module's rewritten tests; `test_quest_dashboard.py`: no dossier link, direction tree and frontier tree present, link from gr510505 fixed; `test_refresh_on_retired_quest_logs_and_mints_no_dossier`; the provenance-strip test with and without a card |
| 9 | Ops: step-0 `--all` dry run and retire `--all` dry run to Reto; on his go, step 0 apply, then retire apply on dormant quests, then active ones between ticks; skills ship | ops | Sonnet (cluster-admin) | dry-run reports attached to gr510505; post-apply census shows 0 live `dossier-of` links, every live quest's `meta.retired_dossier_id` and `meta.key_ideas_extracted_at` set |
| 10 | Delete: dossier storage code, `ledger_ops`/`dialectic_ops`/`dossier_text` schema keys, `view='dossier'` alias, ATTEMPT axis, machine-owned refusals, dossier tests, skills/glossary/manual rewrite, relation-refusal migration, superseded backlog items | M | Sonnet | `rtk proxy grep -rli dossier src` lists only sealed migrations and the retire tool; skill-eval corpus has no dossier answers |

Removal gates: the `ledger_ops`/`dialectic_ops`/`dossier_text` prompt
schema goes only after slice 9 (no unflagged quest left);
`view='dossier'` alias and skills after 9; `dossier.py` and its tests in 10
(`--undo` goes with them: after 10 there is no dossier path to return to);
the relation-refusal migration after 10, via builder-ce.
`quest-graph-as-dossier.md`, `quest-dossier-dialectic.md`,
`dossier-present-tense-refinement.md`, `topic-report-quests.md`,
`dossier-paper-handles-emitted-bare-not-bracketed.md` are deleted in 10 (their
surviving design input is in this file or in code).

## Risks

- **Information in narratives.** The narrative is the only home of
  interpretive claims that never became a finding or logbook entry.
  Mitigation: step 0 mints the key claims as findings with their cites; the
  full text is archived as one logbook entry per quest (searchable, never in
  a prompt); the synthesis entry (Q1) carries interpretation forward. Loss
  accepted: per-paragraph chunk embeddings of narratives (99 chunks).
- **Invented key ideas.** An LLM extraction can state what the dossier did
  not. Mitigation: the verbatim-span check drops any claim without one;
  Reto reads the step-0 dry run.
- **Quests mid-flight.** A tick that started on the dossier path and
  finishes after the flag flips would `rewrite_dossier` into a retired
  draft or resurrect one. Mitigation: the in-flight check, the apply/finish
  flag re-read, and `ensure_dossier`'s refusal (§Mid-flight quests; slice 4
  test).
- **Todo-surface flood.** 1376 direction todos land on prod. Without the
  rotation fence they would appear in doable/nursery/hub counts and draw
  workers. Fence ships in slice 2, before any todo is written.
- **Wide fisheye on every open.** Mitigation: balanced TOC clusters any ring
  over k (~2.1k tokens to any direction on qu202467, independent of root
  count); `nest` keeps childless roots ≤ 40 live as tick hygiene. Residual:
  keyword labels can mislead (label hit-rate 0.76 to 0.86 lexical), costing
  a backtrack call; `under=` search is the escape.
- **Evidence miscounted after re-home.** A reader that still counts only
  direct quest `serves` edges would see a quest lose evidence. Mitigation:
  slice 5 switches every reader to `quest_evidence` before the tool moves
  an edge, with a before/after count test.
- **Prompt drift.** Direction text addressed by `td` handle is new to the
  model; quoted-text addressing stays as fallback so the gr263256 failure
  (unaddressable nodes re-added every tick) cannot return. Watch the tick's
  applied-ops log line on qu164903 for three ticks after flip.
- **Agents with stale skills.** Cluster agents and saved prompts say
  "dossier", `view='dossier'`, `rel='dossier-of'`. Mitigation: alias view
  with a rename note until slice 10; the link door refusal names `part-of`/
  `serves` in its `next=`; skills ship in slice 9 before the alias is cut.
- **Retired draft ids in old links.** 35 `related-to` rows and logbook
  `[dr…]` cites point at retired dossier drafts; they render as retired
  refs, which is the existing behaviour.
- **Invented card content.** Mitigation: the writer and the converter
  only quote (substring of a source chunk or a structure meta value) and
  write `not recorded` otherwise; no LLM in either path; slice 2c's test
  asserts the substring property on every line.
- **Card flood in memory search.** ~300 cards per materials quest land in
  `SPACE:research`, where recall fusion and the dream frontier read. They
  are real research memory and stay searchable; `card:experiment` lets a
  reader exclude them, and no `DREAM:`/`auto_refresh_days` keeps them out
  of consolidation and staleness (§Open for Reto 4).
- **Concurrent card writes.** Two harvests or a harvest and an agent edit
  on one card: the body is one chunk replaced whole. The writer re-reads
  inside its transaction, appends, and skips sources already in
  `card_sources`; an agent's line is never rewritten.

## Open for Reto (cards)

Only what the design cannot settle; each with the recommended answer,
which slice 2b builds unless he says otherwise.

1. **Card → structure/pathway rel.** `related-to` (recommended: exists,
   renders today, identified by the `card:experiment` tag, no migration)
   vs a new typed `card-of` relation (one migration through BUILDER, a
   ring-group entry, a `precis-relations` line).
2. **No LLM in the writer or the converter.** Recommended: deterministic
   quotes only, so "never inferred" holds by construction; the cost is a
   `Next` section that is only ever quoted decisions, never a synthesis.
   An optional LLM `Next` pass can come later as its own slice.
3. **Does the tick read the cards?** Recommended: not in passes 2–4. The
   prompt keeps the logbook tail + synthesis entry; cards are written, and
   the structure fisheye is where an agent reads one. A "cards of the
   frontier rows" prompt section is a later slice, after Reto has read
   qu164903's converted cards.
4. **Cards in default memory recall.** Recommended: included (they are
   research memory), excluded from the dream frontier by having no
   `DREAM:` tag. The alternative, hiding `card:experiment` from
   `search(kind='memory')` unless asked for, changes recall semantics for
   every space.
5. **The ladder keeps auto-committing.** Recommended: yes, unchanged in
   behaviour; it now logs the rung's proposals as hypothesis entries so a
   card can quote the model's rationale, and the card's `Why` says
   `auto-committed`. The alternative is refusing a rung whose proposal has
   no `rationale`, which would re-open the stall the ladder exists to
   break.

## Decisions

"Knowledge graph" here means the refs and their typed links: papers,
findings, measures, todos, quests, memories. All folded into the body above.

**Reto 2026-11-01, gr549581 ("file it"):** one memory card per experiment,
under a direction todo; todos stay the direction tree with status and
do-not-repropose; the card holds the why, expected outcome, result and
next ideas and links to the cards before and after it; an experiment is a
candidate structure, not a pathway (candidates with several pathways share
one card); a pathway "should lead into the memory graph, to a current key
memory … deeply meshed under the quest … and not to a draft. A fisheye
view is fine if it links back to the pathway view." Passes 1–4 as in the
intro; HOLD on building until this design pass is done.

**Reto 2026-11-02, gr549581 comment 1:** start the design pass now; fold
gr553318 in — "the quest writer's typed edges from log entries to the
structures they name, grouped why/plan/results/analysis/lineage on the
structure fisheye, are the same mesh as the experiment cards." →
§Logbook edges; the grouping is the card's sections.

**Reto 2026-10-24, coordinator brief:** "Retire the Dossiers. These move into
the knowledge graph. Generally we want to enhance the graph so its easy to
fisheye." Lifts the earlier hold ("Lets not retire the dossiers and just
clean up and wind up", same day, ~11:53Z); confirmed in chat at ~14:00Z.

**Reto 2026-10-24, via CHAT (verbatim):**

- Goal: "The goal is to make it navigable to the LLM, not necessarily the
  user. Do we need to adjust fisheye restrictions, or make a few ... graph
  nodes that allow rapid traversal (an index like structure with fanout?"
  → §Quest navigation: (a) + scoped search, strands as the index.
- Weave: "yes see if a weave like thing does the graph work. If so delete it
  otherwise port it." → deleted (Q8 below).

**Reto 2026-10-24:** approved as specified, including the tick `nest` op
opening umbrella strand todos above 40 live childless roots (was open
question 1). Same day, on the spike: `under=` follows the house convention
(§`under=` semantics); "I really like toc on fisheye ... If we can
well-balance the TOC tree (we are allowed to!) then it's super awesome" →
§Balanced TOC, slice 3b.

**Reto 2026-10-24, chat-99:**

- **Q1–Q3:** as recommended (one write-once synthesis entry per tick; map
  onto existing todo `STATUS`; migrate many-root quests mechanically).
- **Q4 narratives:** archive as recommended, **and** analyse each narrative
  for its key insights and mint them as graph nodes (findings or hypotheses,
  cited where the narrative cites), linked to the quest's directions. The
  archive keeps the prose; the graph gets the content. → step 0.
- **Q5 export draft:** no persisted export draft. Reto: the graph is
  optimised for reasoning, fact representation and discovery, while exports
  are specific formats covering specific subsets in specific forms (a paper,
  an executive report, …), so a whole-quest export render is hard and of
  little use. An export is a writer step: a writer picks the subset from the
  quest's graph and writes a normal draft that `serves` the quest, exported
  by the existing draft export. **Confirmed by Reto 2026-10-24.**
- **Q6 dossier drafts:** soft-retire, **after** extracting their key ideas
  into the graph (same pass as Q4). The quest may have ended; the
  information stays relevant.
- **Q7 dormant quests:** migrate all, and migrate their key ideas into the
  graph with links (same pass as Q4/Q6).
- **Q8 weave:** weave writes into the knowledge graph now that the dossier
  is retired. If an existing path already places papers and findings into
  the graph that way, delete weave instead (git keeps it). Checked
  2026-10-24: `workers/hub_refine.py` stage 5 *widen* and
  `taproot/hub.py::attach_evidence`/`apply_placement` already place papers
  into the graph as evidence edges; weave's prose recomposition has no graph
  target; its off-topic/superseded dispositions are dropped. The one gap —
  no seed for quest-serving papers with no inbound evidence edge — is
  slice 7's `hub_refine` quest seed. **Delete.**
- **Q9 unplaced evidence:** no new strands from clusters, as recommended,
  but keep looking for ways the graph can absorb it (re-home re-runs, the
  gardener, the castor finding reader, slice 7's quest seed).

## Links

- gr549581 (experiment memory cards, Reto 2026-11-01/02; passes 1–4) ·
  gr553318 (logbook entry → structure edges, folded in) · gr549580 (never
  a draft link from the pathway page) · gr541346 (quest-page dossier
  block removed) ·
- gr510505 (ruling + the shape proposed in `chat-99`) ·
  `quest-graph-as-dossier.md` (2026-10-01 ruling) ·
  `paper-writing-pipeline.md` (export = writer step; embed target for
  `quest-artifacts-in-dossier.md`), `draft-refresh.md` (narrative gate
  reuse) · skill `precis-fisheye-help` (fisheye ring groups).
