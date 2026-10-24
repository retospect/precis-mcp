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
open questions (relation, hierarchy, linearisation trigger).

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
| finding/measure context in prose | finding/measure `serves` the quest or a strand/direction todo |
| narrative chunks, LLM-rewritten each tick | `precis.quest.view.render_narrative` — generated from the bounded neighbourhood, cached by fingerprint |
| `frontier-tree` pinned chunk, rewritten each tick | `frontier.render_frontier_tree` rendered on the quest page and `view='frontier'` |
| dialectic blocks | hypothesis findings + `supports`/`contradicts`/`tests` edges (already minted at apply time) |
| `capability-ledger` pinned chunk (roadmap root) | rendered on read from `roadmap_ledger` |
| `paper-of` draft (0 links on prod) | export draft built from the graph at export time |

**Membership.** `serves` keeps its meaning (work/knowledge that advances the
quest: 3528 papers, 1112 findings, 682 structures, 44 todos on prod).
`part-of` is new on quests and means "a direction of this quest's attempt
tree"; it is what separates a direction from a work todo that `serves`.
`part-of` is the inverse of `contains` (acyclic, migration 0180); no
constraint change.

**Strand.** A todo `part-of` a quest that has child todos. A direction is
any todo in that tree. A root with no children is a direction directly
`part-of` the quest. No synthetic strand is minted.

**Status mapping** (todo `STATUS` vocabulary is closed and trigger-enforced,
`store/types.py::_KIND_STATUS_VALUES`, migration 0176; it has no
`tried`/`ruled-out`):

| ledger `ATTEMPT:` | todo `STATUS` | do-not-repropose |
|---|---|---|
| `open` | `open` | no |
| `active` | `doing` | no |
| `tried` | `done` | yes |
| `ruled-out` | `won't-do` | yes |
| `idea` (registered, never written) | `open` | no |

The mapping is a bijection, so no second axis is needed and the queue
predicates (`precis.todo.TERMINAL_TODO_STATUSES`) stay correct. The model
still speaks `open/active/tried/ruled-out`; `precis.quest.directions` maps at
the boundary. Open question 2 covers widening the vocabulary instead.

**Rotation fence.** Direction todos are not work: they must never enter
`view='doable'`/`'queue'`, the nursery, hub counts or `gaps.quest_momentum`'s
`open_todo_servers`. The fence is "reachable from a quest by `part-of` +
`parent_id`", applied in `precis/todo/views.py` and the nursery sweep.

## Narrative view and its bound

`precis.quest.view` (new):

- `quest_neighbourhood(store, qid)` — strands with their direction subtrees,
  findings/measures/structures `serves` the quest or any direction, hypothesis
  findings with their `supports`/`contradicts`/`tests` edges, the newest
  `synthesis` logbook entry (open question 1). Bounded per group: newest
  first, the `utils/eye_render.py` group-cap pattern.
- `fingerprint(nb)` — sha over `(ref_id, status, updated_at)` of every
  selected node; one SQL round trip.
- `render_narrative(nb, cfg)` — deterministic markdown; fills by node count,
  never truncates a node's text (the gr263256 quotability rule
  `dossier.ledger_open_nodes` already follows).

Cache: render on read, in-process LRU keyed `(qid, fingerprint)`;
`meta.narrative_view = {fingerprint, words, node_ids_sha, nodes}` on the
quest is the only
persisted state (no bulky text in quest meta; `tick.py`'s checkpoint note).
A changed fingerprint is "change"; nothing else regenerates it.

**The bound.** `narrative_budget.NarrativeBudgetConfig.ceiling_words` (2500
default; per-quest `meta.dossier.narrative_word_ceiling`, read under a new
`meta.narrative.*` key with the old one as fallback). Enforced in
`render_narrative`: word budget = `ceiling_words` when the selected node set
changed since `meta.narrative_view` (new ids, not only edits or status
flips — that is the progress evidence), else `min(ceiling_words, prev × 1.15 + 50)`
— `narrative_growth_gate`'s ratchet applied as a fill budget, so a rejected
rewrite cannot happen and no compress re-prompt is needed. The open/do-not-
repropose lists keep their own bound,
`dossier._OPEN_LEDGER_BUDGET_CHARS_DEFAULT` (12 000 chars), moved to
`directions`. `narrative_growth_gate` itself stays: `draft_refresh` and the
synthesis entry still use it.

**Pinned by** (new tests): in `tests/test_quest_view.py`,
`test_narrative_within_ceiling` (fixture: 500 directions, 300 serving
findings → output ≤ `ceiling_words`),
`test_no_new_nodes_caps_growth_at_ratchet` and
`test_fingerprint_stable_without_change`; in `tests/test_quest_tick.py`,
`test_prompt_context_sections_bounded`: on the same fixture, `build_tick_prompt`'s narrative + directions sections are ≤
`ceiling_words` words + 12 000 chars, the old narrative + ledger bound, so
`quest_tick`'s context does not grow.

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
  re-creates a dossier via `ensure_dossier` if none exists. Slice 5 moves
  those to logbook entries.
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
with that text as the reason; the rows stay. Internal `create_draft(...,
relation=)` writes bypass the doors, so the code guard is slice 2's
`ensure_dossier` refusal. Number assigned by builder-ce at land time.

**Tool:** `precis quest retire-dossier <qid>|--all [--apply]`
(`src/precis/quest/retire.py`), dry run by default. Per quest, one
transaction:

1. Ledger forest (`dossier.read_ledger_nodes`) → todos in DFS order: roots
   `part-of` the quest, children by `parent_id`; status by the mapping
   table; `meta.from_ledger_chunk=<handle>` makes a re-run skip what exists.
   `parent_unresolved` survives as `meta.parent_unresolved`. Depth > 10 (the
   todo wall) attaches at depth 10 with `meta.flattened_from_depth`.
2. Narrative → one logbook entry (`entry_type='note'`, `by=migration`),
   full text, `meta.archived_narrative=true`, `meta.retired_dossier_id`.
   `tick._logbook_tail` and the view skip archived entries, so qu175733's
   9879 words never reach a prompt.
3. Dialectic entries: support/counter already exist as edges; each
   `experiment` entry → a direction todo under its hypothesis's strand
   (`open`).
4. `frontier-tree`, `capability-ledger`, seed chunks: dropped (code-
   generated, re-renderable).
5. Dossier draft: soft-retired (`retired_at`), `dossier-of` link rows
   deleted, the 35 inbound `related-to` rows kept (they resolve to a
   retired ref, like any retired ref). Hard deletes of the draft: not done.
6. Stamp `meta.dossier_retired=true` on the quest — the cutover flag.

The dry run prints per quest: nodes by status → todos, roots/strands,
max depth and flattened count, narrative words → archive entry, dialectic
entries → todos, chunks dropped by family, links removed, inbound links
kept, drafts that `serve` the quest (untouched). Totals at the end. Reto
reads the `--all` dry run before any `--apply`.

**Mid-flight quests.** `--apply` takes the quest's coordinator lease
(the `quest_tick` claim) or refuses with "tick in flight, retry after
<eta>". Each quest flips individually; ticks between quests run the old or
new path by the flag.

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
| `quest/tick.py` | `build_tick_prompt` reads narrative, ledger forest, dialectic; `_apply_narrative_gate`; apply stage runs `ledger_ops`→`add_attempt`/`mark_attempt`, `append_ledger_entry`, `dialectic_ops`; finish stage `update_frontier_tree`, `rewrite_dossier` | prompt reads `view.render_narrative` + `directions` lists; `direction_ops` (add/mark by `td` handle or quoted text) → `directions`; `dossier_text` replaced by one `synthesis` entry (Q1); finish drops both dossier writes |
| `workers/job_types/quest_tick.py` | `outcome.dossier_rewritten` counts a tick as substantive (dry-tick/empty-punt detection, `_phase_tick`) | substantive = a direction op applied or a synthesis entry written (`TickOutcome.directions_changed`) |
| `quest/rulings.py` | `dossier_ref_id` + dialectic experiment entries → measurement rulings | experiment direction todos + hypothesis findings; `tests` edge unchanged |
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

**Untouched:** sealed migrations `0065`, `0067`, `0085`, `0089`, `0142`,
`0180`, `baseline/schema.sql`.

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
`paper-of`, new `part-of` on quests), `precis-roadmap-help`; one-line edits
in `precis-overview`, `precis-tags` (`ATTEMPT:`), `precis-draft-help`,
`precis-orcid-help`, `precis-paper-tag-axes`, `precis-estimate-help`;
manual `04-watching-the-loop.md`; glossary `dossier` → retired pointer,
`frontier tree`, `role`, `ratchet`.

## Slices

Every slice lands green with the dossier path still working for unflagged
quests.

| # | Slice | Size | Acceptance |
|---|---|---|---|
| 1 | `quest/directions.py` (todo-backed list/add/mark, near-dup upsert, do-not-repropose + open renders; text logic moved from `dossier.py`, which re-imports it) and `quest/view.py` (neighbourhood, fingerprint, `render_narrative`); `view='directions'`/`'narrative'` on quest; rotation fence | M | `test_quest_directions.py` ports every `test_dossier_hygiene.py` assertion onto todos (Rh/Ru veto, prefix match ≥ 40 chars, never-regress merge, ancestor shadow); `test_quest_view.py` (the three bound tests); `test_todo_views` case: a direction todo is absent from `doable`/`queue`/nursery |
| 2 | Tick on the flag: `meta.dossier_retired` quests (and every quest without a dossier) use directions + view + `direction_ops` + the synthesis entry; `ensure_dossier` and every writer refuse on a flagged quest | L | `test_quest_tick.py`: flagged fixture runs llm→finish with no `dossier-of` link created; `test_prompt_context_sections_bounded`; `test_no_dossier_resurrection` (each former `ensure_dossier` caller on a flagged quest creates nothing); unflagged fixtures unchanged |
| 3 | `retire-dossier` tool, dry run + apply, lease check | M | `test_retire_dossier.py`: lossless round trip (forest == todo tree, statuses by the table), idempotent re-run, archived entry excluded from `_logbook_tail`, `test_serving_draft_untouched`, refuses while a tick holds the lease |
| 4 | Web + export: quest page narrative/directions/frontier tree, export draft + Paper button, `meta.generated_from_quest` machine-owned guard | M | `test_quest_dashboard.py`: no dossier link, frontier tree present, link from gr510505 fixed; `test_quest_export.py`: generated draft exports via `export_docx`, rebuild replaces chunks, handler refuses hand edits |
| 5 | Remaining consumers: rulings, roadmap capability ledger, `draft_refresh`, weave target, compute reset, search/review guards | L (split a–d by module) | each module's rewritten tests; `test_refresh_on_retired_quest_logs_and_mints_no_dossier` |
| 6 | Ops: `--all` dry run to Reto; on his go, apply dormant quests, then active ones between ticks | ops | dry-run report attached to gr510505; post-apply census shows 0 live `dossier-of` links |
| 7 | Delete: dossier storage code, `ledger_ops`/`dialectic_ops`/`dossier_text` schema keys, `view='dossier'` alias, ATTEMPT axis, dossier tests, skills/glossary/manual rewrite, relation-refusal migration, superseded backlog items | M | `rtk proxy grep -rli dossier src` lists only sealed migrations and the retire tool; skill-eval corpus has no dossier answers |

Removal gates: the `ledger_ops`/`dialectic_ops`/`dossier_text` prompt
schema goes only after slice 6 (no unflagged quest left);
`view='dossier'` alias and skills after 6; `dossier.py` and its tests in 7;
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
  draft or resurrect one. Mitigation: apply under the coordinator lease;
  `ensure_dossier` refuses on flagged quests (slice 2 test).
- **Todo-surface flood.** 1376 direction todos land on prod. Without the
  rotation fence they would appear in doable/nursery/hub counts and draw
  workers. Fence ships in slice 1, before any todo is written.
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

1. **Does the tick still write prose?** Rec: yes, one `synthesis` logbook
   entry per tick (WORM, ≤ `target_words`, gated by `narrative_growth_gate`
   against the previous synthesis entry); the view shows only the newest.
   Interpretation carries forward without a mutable document.
2. **Status vocabulary.** Rec: map onto existing todo `STATUS` (table
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

## Links

- gr510505 (ruling + chat-99 shape) · `quest-graph-as-dossier.md` (2026-10-01
  ruling) · `paper-writing-pipeline.md`, `draft-refresh.md` (narrative gate
  reuse) · `quest-artifacts-in-dossier.md` (embed target → export draft).
