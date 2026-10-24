---
status: draft
pillar: quests
prio: high
title: Retire the dossier — the quest's typed edges carry the synthesis, every document is a render
---

# Retire the dossier

Reto 2026-10-24 (gr510505): "retire the dossier". Refines Reto 2026-10-01
(`quest-graph-as-dossier.md`): "we want a graph that 'belongs to' the quest,
and it 'is' the dossier. We can linearize it with a writer agent, but we
manage the things in the mesh, with a hierarchy of 'belongs to quest' or
something with the quest node at apex." This spec answers that item's three
open questions (relation, hierarchy, linearisation trigger). A second
2026-10-24 rule, relayed from Reto's chat session `chat-99`: evidence
(findings, measures, papers) `serves` the narrowest todo or question it
answers, never the quest directly, and the existing direct quest links get a
one-off re-home (migration step 6).

Retires `src/precis/quest/dossier.py`'s storage: the `draft` linked
`dossier-of` to a quest, its rewritten narrative, its pinned ledger
(`meta.pinned='ledger'`/`'ledger-node'` chunks + `ATTEMPT:` chunk tags),
its pinned `frontier-tree`, `dialectic*` and `capability-ledger` chunks, and
the unused `paper-of` projection.

## Target shape

| Today (dossier) | After |
|---|---|
| `dossier-of` draft per quest | none; no dossier tag, no bundle node |
| pinned ledger node | direction todo, `part-of` the quest (root) or nested under a strand by `parent_id` |
| `ATTEMPT:<status>` chunk tag | todo `STATUS` (mapping below) |
| `ledger_do_not_repropose` over the forest | query over the quest's direction todos in `done`/`won't-do`, same render rules |
| top-level ledger nodes with children | strand todos — the only index nodes |
| finding/measure/paper `serves` the quest | `serves` the narrowest direction or strand todo it answers; the quest only when none fits (step 6) |
| narrative chunks, LLM-rewritten each tick | `precis.quest.view.render_narrative` — generated from the bounded neighbourhood, cached by fingerprint |
| `frontier-tree` pinned chunk, rewritten each tick | `frontier.render_frontier_tree` rendered on the quest page and `view='frontier'` |
| dialectic blocks | hypothesis findings + `supports`/`disputes`/`tests` edges (`supports`/`disputes` minted at apply time, `dossier.py::_DIALECTIC_EDGE_RELATION`; `tests` by `rulings.mint_measurement_rulings`) |
| `capability-ledger` pinned chunk (roadmap root) | rendered on read from `roadmap_ledger` |
| `paper-of` draft (0 links on prod) | export draft built from the graph at export time |

**Membership.** `serves` keeps its meaning (work/knowledge that advances the
quest: 3528 papers, 1112 findings, 682 structures, 44 todos serve quests on
prod today), with Reto's narrowest-target rule: evidence serves the direction
or strand it answers. `part-of` is new on quests and means "a direction of
this quest's attempt tree"; it is what separates a direction from a work
todo that `serves`.

**The `part-of` door.** Migration 0095 registered `part-of` ↔ `contains` for
components ("Source component is structurally part of target component.")
before 0180 added the constraint columns; 0180 sets `contains` acyclic +
transitive and touches no other column of either row. So both rows have
`domain_kinds`/`range_kinds` NULL, and `check_relation_constraints`
(`utils/link_tag_ops.py`) checks a `part-of` write as `contains` with its
ends swapped: todo→quest passes the kind check, and the acyclic walk refuses
a quest that would end up inside its own direction. No constraint change.
Slice 1 pins it: `test_part_of_todo_to_quest_passes_constraints` in
`tests/test_relation_constraints.py` (todo→quest accepted; quest→todo after
it refused as a cycle). If that test fails, the fix is a constraint-row
migration landed through builder-ce before slice 3 writes any edge. The
component wording of the description only shows in a refusal message; slice
7's migration rewrites it kind-neutral. `Store.part_of_descendants`
(`store/_refs_ops.py`) already walks this tree, both orientations.

**Strand.** A todo `part-of` a quest that has child todos. A direction is
any todo in that tree. A root with no children is a direction directly
`part-of` the quest. No synthetic strand is minted.

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
maps at the boundary. Open question 2 covers widening the vocabulary instead.

**Rotation fence.** Direction todos are not work: they must never enter
`view='doable'`/`'queue'`, the nursery, hub counts or `gaps.quest_momentum`'s
`open_todo_servers`. The fence is "reachable from a quest by `part-of` +
`parent_id`", applied in `precis/todo/views.py` and the nursery sweep.

## Narrative view and its bound

`precis.quest.view` (new):

- `quest_neighbourhood(store, qid)` — the quest's graph, bounded per group,
  each group ordered as stated and closed by a visible `… +N more` line (the
  `utils/eye_render.py` `_NEIGHBOR_GROUP_CAP` pattern, no silent cap):

  | group | cap | order |
  |---|---|---|
  | strands | 20 | newest `updated_at` first |
  | directions per strand | 12 | `doing`, `open`, then terminal, newest first |
  | root directions (no children) | 40 | same |
  | findings `serves` the quest or a direction | 40 | newest first |
  | measures, same | 20 | newest first |
  | structures, same | 20 | newest first |
  | hypothesis findings | 15 | open first, newest first |
  | evidence edges per hypothesis | 5 | newest first |
  | synthesis entry | 1 | the newest |

  At most 471 nodes. The synthesis entry is a logbook `reflection` with
  `meta.synthesis=true` (slice 2, open question 1).
- `fingerprint(nb)` — sha over `(ref_id, status, updated_at)` of every
  selected node; one SQL round trip.
- `render_narrative(nb, cfg)` — deterministic markdown, filled group by
  group in the order above. A node is rendered whole or not at all: a node
  whose text is longer than the remaining word budget is skipped and counted
  in its group's `+N more` line, and filling continues with the next node.
  So no node's text is truncated (the gr263256 quotability rule
  `dossier.ledger_open_nodes` already follows) and the output is still
  ≤ the word budget.

Cache: render on read, in-process LRU keyed `(qid, fingerprint)`. The only
persisted state is `meta.narrative_view = {fingerprint, words,
node_ids_sha, nodes}` on the quest (no bulky text in quest meta; `tick.py`'s
checkpoint note). A changed fingerprint is "change"; nothing else
regenerates it.

**The bound.** `narrative_budget.NarrativeBudgetConfig.ceiling_words` (2500
default; per-quest `meta.dossier.narrative_word_ceiling`, read under a new
`meta.narrative.*` key with the old one as fallback). `render_narrative`
enforces it as a word budget: `ceiling_words` when the selected node set
changed since `meta.narrative_view` (new ids, not only edits or status flips
— that is the progress evidence), else `min(ceiling_words, prev × 1.15 +
50)`. That is `narrative_growth_gate`'s ratchet applied as a fill budget, so
a rejected rewrite cannot happen and no compress re-prompt is needed. The
open/do-not-repropose lists keep their own bound,
`dossier._OPEN_LEDGER_BUDGET_CHARS_DEFAULT` (12 000 chars), moved to
`directions`. `narrative_growth_gate` itself stays: `draft_refresh` and the
synthesis entry still use it.

**Pinned by** (new tests): in `tests/test_quest_view.py`,
`test_narrative_within_ceiling` (fixture: 500 directions, 300 serving
findings, one 670-char direction placed when 50 words of budget remain →
output ≤ `ceiling_words`, that direction absent and counted in `+N more`,
every rendered node's text whole), `test_neighbourhood_group_caps` (each
group at its cap + overflow line), `test_no_new_nodes_caps_growth_at_ratchet`
and `test_fingerprint_stable_without_change`; in `tests/test_quest_tick.py`,
`test_prompt_context_sections_bounded`: on the same fixture,
`build_tick_prompt`'s narrative + directions sections are ≤ `ceiling_words`
words + 12 000 chars, the old narrative + ledger bound, so `quest_tick`'s
context does not grow.

## Export

`/drafts/<id>/export.docx` (`export/docx.py::export_docx`) and the PDF
`draft_export` job are draft-generic; no export code reads the dossier
(`grep -n dossier src/precis/export` is empty).

- **Quest export.** `precis.quest.export.build_export_draft(store, qid)`
  writes one generated draft per quest from the graph at export time
  (strands → sections, directions → status lists, serving findings with
  their evidence → paragraphs with `[pa…]`/`[fi…]` cites, frontier figures
  via `quest/figures.py`). Chunks are DELETE + INSERT per build. It is
  linked `serves` the quest with `meta.generated_from_quest`, and
  `DraftHandler._refuse_if_machine_owned` keys on that meta instead of the
  `dossier-of`/`paper-of` relation. The quest page's Paper button builds
  then redirects to the draft's `export.docx`/`export.pdf`.
  `quest-artifacts-in-dossier.md` (pareto/energy/slab embeds) retargets here.
- **Nanobuds (dr173020).** A hand-authored draft: `draft-of` td173019,
  `serves` qu459585, no `dossier-of`/`paper-of` edge. Its export never
  touches the dossier. The one coupling is `draft_refresh`: a refresh of any
  draft that `serves` a quest calls `dossier.add_attempt`/`mark_attempt` on
  that quest (`workers/job_types/draft_refresh.py::_dispatch`), which also
  re-creates a dossier via `ensure_dossier` if none exists. Slice 2's
  refusal stops the re-creation; slice 5 moves the calls to logbook entries.
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
relation=)` writes bypass the doors, so the code guard is slice 2's
`ensure_dossier` refusal. Number assigned by builder-ce at land time.

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
6. **Re-home evidence.** For every live finding, measure and paper with a
   `serves` edge to the quest, pick a home among the quest's direction and
   strand todos by the first rule that yields one:
   - a. *Hypothesis edge.* The evidence has a `supports`, `disputes`,
     `contradicts` or `tests` edge to a hypothesis finding H, and H has
     experiment direction todos (`meta.hypothesis_id=H`, step 3): home = the
     narrowest todo containing all of them (the one todo, or their lowest
     common ancestor). H itself homes by the same rule.
   - b. *Text match.* `directions._match_nodes(forest, <evidence title>)`:
     exact match, then the ≥ 40-char unique-prefix pass.
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
   no count; slice 3 lands that switch with the tool. `--rehome <qid>` runs
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
archived-narrative entry stays (WORM); the view skips it. Since step 4
deletes no chunk, the dossier comes back as it was at apply time. Direction
edits made between apply and undo (status changes, new directions) are not
written back to the ledger; the undo report lists them. Chosen over keeping
the link rows until slice 7: `dossier_ref_id` ignores a retired draft, so
live link rows alone restore nothing, and the minted todos would still need
retiring, so an undo has to exist either way; this makes it one tested
command. Test: `test_apply_undo_apply_round_trip` — after apply → undo, the
dossier's ledger forest, chunk set, links and quest meta equal the
pre-apply snapshot and no minted todo is live; the second apply yields the
same todo tree (by `meta.from_ledger_chunk`), the same re-homed edges and
still one archive entry.

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
   re-read, hits `ensure_dossier`'s refusal (slice 2): the write is logged
   and dropped, never landed in a dossier.

Each quest flips individually; ticks between quests run the old or new path
by the flag.

## Module fates

Grep of `src/` + `scripts/` (non-pycache): 73 files mention "dossier", 49
of them Python. Every file is in exactly one group below.

**Deleted (last slice):** `quest/dossier.py` storage — `ensure_dossier`,
`ensure_ledger_chunk`, `rewrite_dossier`, `read_dossier`, `read_narrative`,
`read_ledger`, the ledger chunk/tag writers, legacy-ledger migration,
`update_frontier_tree`, the dialectic block storage, `paper_ref_id`,
`dossier_ref_id`. The text logic moves to `quest/directions.py` first
(slice 1): `_match_nodes`, `_find_near_dup_node`, `_elements_conflict`,
`_is_near_dup`, `_sig_tokens`, `_do_not_repropose_lines`, the open-list
budget. `cli/quest.py::_cmd_dossier`/`_cmd_dossier_dedup` (replaced by
`retire-dossier` and `view='directions'`). `store/types.py`'s `ATTEMPT`
axis entry and the `dossier-of`/`paper-of` relation-registry rows in Python.

**Rewritten onto edges/views:**

| Module | Today | After |
|---|---|---|
| `quest/tick.py` | `build_tick_prompt` reads narrative, ledger forest, dialectic; `_apply_narrative_gate`; apply stage runs `ledger_ops`→`add_attempt`/`mark_attempt`, `append_ledger_entry`, `dialectic_ops`; finish stage `update_frontier_tree`, `rewrite_dossier` | prompt reads `view.render_narrative` + `directions` lists; `direction_ops` (add/mark by `td` handle or quoted text) → `directions`; `dossier_text` replaced by one synthesis entry (Q1); finish drops both dossier writes; apply and finish re-read the flag |
| `workers/job_types/quest_tick.py` | `outcome.dossier_rewritten` counts a tick as substantive (dry-tick/empty-punt detection, `_phase_tick`) | substantive = a direction op applied or a synthesis entry written (`TickOutcome.directions_changed`) |
| `quest/rulings.py` | `dossier_ref_id` + dialectic experiment entries → measurement rulings | experiment direction todos (`meta.hypothesis_id`) + hypothesis findings; `tests` edge unchanged |
| `quest/roadmap_tick.py` | `ensure_dossier`, regenerates `capability-ledger` chunk | quest view/page renders `roadmap_ledger` output on read |
| `quest/roadmap_ledger.py` | docstring: pins into the root's dossier | render target = quest view (pure module, prose only) |
| `quest/frontier.py` | tree rendered into the dossier chunk | `render_frontier_tree` called by the quest page and `view='frontier'` |
| `quest/weave.py`, `weave_tick.py`, `placement.py`, `residual_cluster.py`, `weave_review.py`, `citation_mint.py` | weave-body quests weave papers into the dossier's sections | target = the draft named by quest `meta.weave_draft_id`, a draft that `serves` the quest (Q8); 0 weave-body quests on prod |
| `quest/compute.py` | `reset_compute` stubs the narrative unless `keep_dossier` | resets `meta.narrative_view`; logbook entry only |
| `quest/search.py` | excludes the quest's `dossier-of`/`paper-of` drafts from hits | excludes `meta.generated_from_quest` drafts |
| `quest/review_guard.py`, `review_fanout.py` | machine-owned = `dossier-of`/`paper-of` source | machine-owned = `meta.generated_from_quest` |
| `handlers/draft.py` | `_MACHINE_OWNED_RELATIONS` refusal | same refusal keyed on the meta |
| `handlers/quest.py` | `view='dossier'` (`_render_dossier`), fisheye text | `view='narrative'`, `view='directions'`; `view='dossier'` answers with the narrative plus a one-line rename note until the skills ship |
| `workers/job_types/draft_refresh.py` | attempt label into the owning quest's ledger | logbook `observation` entry; no direction (an LLM failure is not a ruled-out direction) |
| `precis_web/routes/refs.py` | quest page: dossier link, narrative, ledger, `paper_ref_id`; pathway page dossier link | narrative view, strand/direction tree, frontier tree, export button; pathway links the quest page |
| `templates/refs/quest_detail.html.j2`, `pathway_detail.html.j2` | Dossier column, "Open dossier (draft · fisheye)" | Narrative + Directions + Frontier tree sections |
| `precis_web/routes/smartdraft.py` | `dossier-of` in link sort order | entry removed |
| `handlers/_integration_view.py`, `store/_integration_ops.py` | integration ledger over "a topic dossier" draft | any weave target draft (prose + one lookup) |
| `quest/narrative_budget.py` | `meta.dossier.*` overrides | `meta.narrative.*`, old key fallback |

The `serves`-set readers named in step 6 also switch to
`directions.quest_evidence` (slice 3).

**Prose only** (docstring/comment/prompt wording): `quest/__init__.py`
(package invariant "The dossier can't bloat…" rewritten for directions +
view), `quest/cascade.py`, `taproot/reground.py`, `workers/classify.py`,
`classify_topics.py`, `axis_pass.py`, `registry.py`, `cli/classify.py`,
`cli/_worker_passes.py`, `store/protocols.py`, `_chunks_ops.py`,
`_tag_filter.py`, `_draft_review_ops.py`, `handlers/finding.py`,
`precis_web/linkify.py`, `claim_render.py`, `routes/claim.py`,
`templates/claim/_body.html.j2`, `precis_pathway/toon_views.py`,
`precis_estimate/__init__.py`, `utils/llm/json_reply.py`, `utils/units.py`,
`data/topics/co2-conversion.yaml`, `data/axes/README.md`,
`scripts/context-audit/capture.py`, `CATALOG.md`.

**Untouched:** sealed migrations `0065`, `0067`, `0085`, `0089`, `0095`,
`0142`, `0180`, `baseline/schema.sql`.

**Tests** (47 files mention it): deleted with the storage —
`test_dossier_hygiene.py`, the dossier half of `test_quest_dialectic.py`;
rewritten — `test_quest_tick.py` (193 hits), `test_quest_compute.py`,
`test_quest_rulings.py`, `test_quest_roadmap.py`, `test_quest_tick_job.py`,
`test_quest_loop_fixes.py`, `test_quest_cascade.py`, `test_quest_gaps.py`,
`test_weave*.py`, `test_placement.py`, `test_integration_ledger.py`,
`test_review_fanout_writeback.py`, `test_draft_handler.py`,
`precis_web/test_quest_dashboard.py`, `test_pathway_detail.py`,
`test_draft_block_routes_handler_path.py`, `workers/test_draft_refresh_job.py`,
`test_relation_constraints.py`; the rest are incidental strings. A slice that
deletes a test moves its assertion into the replacement test in the same
commit (no loosening; Reto rules on any test that has no replacement).

**Skills:** rewrite `precis-quest-help` (dossier, ledger, `view='dossier'`
sections), `precis-quest-writing-help`, `precis-relations` (`dossier-of`,
`paper-of`, new `part-of` on quests, the narrowest-`serves` rule),
`precis-roadmap-help`; one-line edits in `precis-overview`, `precis-tags`
(`ATTEMPT:`), `precis-draft-help`, `precis-orcid-help`,
`precis-paper-tag-axes`, `precis-estimate-help`; manual
`04-watching-the-loop.md`; glossary `dossier` → retired pointer, `frontier
tree`, `role`, `ratchet`.

## Slices

Every slice lands green with the dossier path still working for quests that
are unflagged and have a dossier.

| # | Slice | Size | Acceptance |
|---|---|---|---|
| 1 | `quest/directions.py` (todo-backed list/add/mark, near-dup upsert, do-not-repropose + open renders, both status maps; text logic moved from `dossier.py`, which re-imports it) and `quest/view.py` (neighbourhood, fingerprint, `render_narrative`); `view='directions'`/`'narrative'` on quest; rotation fence | M | `test_quest_directions.py` ports every `test_dossier_hygiene.py` assertion onto todos (Rh/Ru veto, prefix match ≥ 40 chars, never-regress merge, ancestor shadow) and pins the model-side map (`blocked`/`paused` on the open list); `test_quest_view.py` (the bound tests above); `test_todo_views` case: a direction todo is absent from `doable`/`queue`/nursery; `test_part_of_todo_to_quest_passes_constraints` |
| 2 | Tick on the flag: quests with `meta.dossier_retired` and quests with no dossier use directions + view + `direction_ops` + the synthesis entry; apply and finish re-read the flag; `ensure_dossier` refuses (raises `DossierRetired`) when the quest is flagged **or has no dossier**, so from this slice on it returns existing dossiers and creates none; each pre-slice-5 caller (`roadmap_tick`, `draft_refresh` via `add_attempt`/`mark_attempt`; `rulings` only reads via `dossier_ref_id` and already no-ops without a dossier) catches the refusal and skips its dossier write with a log line | L | Synthesis entry = `entry_type='reflection'` + `meta.synthesis=true`; archive marker = `entry_type='note'` + `meta.archived_narrative=true`; no `ENTRY_TYPES` change, because the vocabulary is listed to the model in the tick prompt and accepted by the handler's `entry=`, model entries carry no meta (`_stage_apply` calls `append_entry` without `extra_meta`) so a model `reflection` never reads as a synthesis, and an added `synthesis` type would invite writes that `clamp_entry_type` would otherwise turn into `note`. Tests in `test_quest_tick.py`: flagged fixture runs llm→finish with no `dossier-of` link created and one synthesis entry; the view picks the synthesis entry over a newer plain `reflection`; `test_prompt_context_sections_bounded`; `test_no_dossier_resurrection` — every `ensure_dossier` path (`update_frontier_tree`, `ensure_ledger_chunk` and its readers/writers, `rewrite_dossier`, `ensure_dialectic_chunk`, `roadmap_tick`, `draft_refresh`) on a flagged quest and on a dossier-less quest creates nothing, plus a tick checkpointed at `tick:apply` and one at `tick:finish` on an unflagged quest, flag flipped, resumed: no dossier write, its ledger ops present as todos, its `dossier_text` as the synthesis entry; unflagged-with-dossier fixtures unchanged |
| 3 | `retire-dossier` tool: dry run, apply (steps 1–7), `--undo`, `--rehome`, in-flight check; `serves`-set readers switched to `directions.quest_evidence` | M | `test_retire_dossier.py`: lossless round trip (forest == todo tree, statuses by the table), idempotent re-run, archived entry excluded from `_logbook_tail`, `test_serving_draft_untouched`, `test_apply_undo_apply_round_trip`, re-home by rule a, b and c, deepest-on-path, disjoint hits stay on the quest and print as ambiguous, refuses while the job is `running` or at `tick:*`; reader tests: quest evidence counts equal before and after a re-home |
| 4 | Web + export: quest page narrative/directions/frontier tree, export draft + Paper button, `meta.generated_from_quest` machine-owned guard | M | `test_quest_dashboard.py`: no dossier link, frontier tree present, link from gr510505 fixed; `test_quest_export.py`: generated draft exports via `export_docx`, rebuild replaces chunks, handler refuses hand edits |
| 5 | Remaining consumers: rulings, roadmap capability ledger, `draft_refresh`, weave target, compute reset, search/review guards | L (split a–d by module) | each module's rewritten tests; `test_refresh_on_retired_quest_logs_and_mints_no_dossier` |
| 6 | Ops: `--all` dry run to Reto; on his go, apply dormant quests, then active ones between ticks | ops | dry-run report attached to gr510505; post-apply census shows 0 live `dossier-of` links and every live quest's `meta.retired_dossier_id` set |
| 7 | Delete: dossier storage code, `ledger_ops`/`dialectic_ops`/`dossier_text` schema keys, `view='dossier'` alias, ATTEMPT axis, dossier tests, skills/glossary/manual rewrite, relation-refusal migration, superseded backlog items | M | `rtk proxy grep -rli dossier src` lists only sealed migrations and the retire tool; skill-eval corpus has no dossier answers |

Removal gates: the `ledger_ops`/`dialectic_ops`/`dossier_text` prompt
schema goes only after slice 6 (no unflagged quest left);
`view='dossier'` alias and skills after 6; `dossier.py` and its tests in 7
(`--undo` goes with them: after 7 there is no dossier path to return to);
the relation-refusal migration after 7, via builder-ce.
`quest-graph-as-dossier.md`, `quest-dossier-dialectic.md`,
`dossier-present-tense-refinement.md`, `topic-report-quests.md`,
`dossier-paper-handles-emitted-bare-not-bracketed.md` are deleted in 7 (their
surviving design input is in this file or in code).

## Risks

- **Information in narratives.** The narrative is the only home of
  interpretive claims that never became a finding or logbook entry.
  Mitigation: the full text is archived as one logbook entry per quest
  (searchable, never in a prompt), and the synthesis entry (Q1) carries
  interpretation forward. Loss accepted: per-paragraph chunk embeddings of
  narratives (99 chunks).
- **Quests mid-flight.** A tick that started on the dossier path and
  finishes after the flag flips would `rewrite_dossier` into a retired
  draft or resurrect one. Mitigation: the in-flight check, the apply/finish
  flag re-read, and `ensure_dossier`'s refusal (§Mid-flight quests; slice 2
  test).
- **Todo-surface flood.** 1376 direction todos land on prod. Without the
  rotation fence they would appear in doable/nursery/hub counts and draw
  workers. Fence ships in slice 1, before any todo is written.
- **Evidence miscounted after re-home.** A reader that still counts only
  direct quest `serves` edges would see a quest lose evidence. Mitigation:
  slice 3 switches every reader to `quest_evidence` before the tool moves
  an edge, with a before/after count test.
- **Prompt drift.** Direction text addressed by `td` handle is new to the
  model; quoted-text addressing stays as fallback so the gr263256 failure
  (unaddressable nodes re-added every tick) cannot return. Watch the tick's
  applied-ops log line on qu164903 for three ticks after flip.
- **Agents with stale skills.** Cluster agents and saved prompts say
  "dossier", `view='dossier'`, `rel='dossier-of'`. Mitigation: alias view
  with a rename note until slice 7; the link door refusal names `part-of`/
  `serves` in its `next=`; skills ship in slice 6 before the alias is cut.
- **Retired draft ids in old links.** 35 `related-to` rows and logbook
  `[dr…]` cites point at retired dossier drafts; they render as retired
  refs, which is the existing behaviour.

## Open questions for Reto

1. **Does the tick still write prose?** Rec: yes, one synthesis entry per
   tick (a `reflection` with `meta.synthesis=true`; WORM, ≤ `target_words`,
   gated by `narrative_growth_gate` against the previous synthesis entry);
   the view shows only the newest. Interpretation carries forward without a
   mutable document.
2. **Status vocabulary.** Rec: map onto existing todo `STATUS` (tables
   above). Alternative: add `tried`/`ruled-out` to todo `STATUS` — a
   migration on the 0176 trigger plus every terminal-status predicate
   (the gr482126 class of bug).
3. **Strands for quests with many roots** (qu202467 200, qu202468 85,
   qu202469 80, qu164903 51). Rec: migrate mechanically (roots with children
   are strands), regroup later only if the page is unreadable.
4. **Narratives.** Rec: archive as one logbook entry, including qu175733's
   9879 words. Alternative: drop.
5. **Export draft.** Rec: persisted, generated, machine-owned, rebuilt per
   export. Alternative: in-memory build passed to `export_docx` (needs an
   exporter refactor off `ref_id`).
6. **Dossier drafts after migration.** Rec: soft-retire. Alternative: keep
   live and read-only.
7. **Dormant quests.** Rec: migrate all 19 live owners; skip the 6 whose
   quest is retired (archive narrative only).
8. **Weave body** (no prod users). Rec: retarget to a `serves` draft named in
   `meta.weave_draft_id`. Alternative: retire weave with the dossier.
9. **Unplaced evidence clusters.** Most of the 3528 papers and 1112 findings
   will likely match no direction by rules a–c. Should the tool mint a
   strand todo for a cluster of unplaced evidence (e.g. by shared topic)?
   Rec: no; the dry run lists unplaced evidence per quest, it stays on the
   quest edge, and `--rehome` re-runs as directions grow. Minting strands
   would invent structure the quest's tick never proposed.

## Links

- gr510505 (ruling + the shape proposed in `chat-99`) ·
  `quest-graph-as-dossier.md` (2026-10-01 ruling) ·
  `paper-writing-pipeline.md`, `draft-refresh.md` (narrative gate reuse) ·
  `quest-artifacts-in-dossier.md` (embed target → export draft).
