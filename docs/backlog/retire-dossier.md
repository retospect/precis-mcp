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
open an umbrella strand (§Quest navigation, open question 1).

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
| qu164903 | 225 | 74 | 545 | 145 (108) | avg 1.9, max 9 |
| qu202468 | 85 | 30 | 223 | 42 (36) | — |

The census below counts 51 roots on qu164903; slice 1 reconciles
(`parent_unresolved` nodes are the likely difference).

**Cost per path** on qu202467. One neighbour line ≈ 30 tokens (handle,
≤ 80-char title, label, status); fisheye header (quest text + other groups)
≈ 1,000; a strand focus ≈ 150 + 30 × children (≤ 540).

| Path to one direction | calls | tokens, worst case | failure mode |
|---|---|---|---|
| today | 2 (quest, `q='contains'`) | ~7,000 | 242 non-root directions unreachable |
| (a) parameterised fisheye: every live root listed, strands first, terminal roots as count lines | 2 live (quest, strand); 3 terminal | ~5,100 live; ~7,500 terminal | 3.5k list paid on every quest open |
| (b) index nodes, fanout 12 (17 nodes) | 3 (quest, index, strand) | ~2,600 | index label is a summary the agent guesses from; a mis-grouped direction is hidden |
| scoped search (`under=` quest) | 2 (search, strand) | ~850 | needs words for the target |

**Decision: (a) + scoped search; no synthetic index nodes.** Strand todos are
the index nodes. (b) saves ≤ 2.5k tokens on the worst path and costs a node
class, a minter, re-grouping as every tick adds and marks directions, a
fence entry, and labels an agent has to guess from; a flat status-tagged
list cannot hide a direction. (a)'s list stays inside one call up to the
hub cap (300 rows). What is wide on prod is the childless roots (85 live on
qu202467), so the tick, which owns directions, keeps them bounded: when a
quest has > 40 live childless roots its prompt carries a `nest` direction op
(move a direction under an existing strand, or open an umbrella strand todo
with ≥ 3 members). The umbrella is a direction the model authored, `part-of`
the quest, inside the rotation fence; no other process mints or maintains
index structure. Open question 1.

**Navigation layer** (slice 3; rendering only, `utils/eye_render.py`,
`utils/refeye.py`, `handlers/quest.py`, `handlers/todo.py`):

1. Quest and strand foci list their directions with the memory-hub branch
   (`_HUB_MEMBER_LABEL`, `_HUB_LIST_CAP`, today keyed on memory
   `section:index`), extended to "quest with `part-of` todos" and "todo with
   `parent_id` children".
2. Order: strands before leaves; within each, `active`, `open`, newest
   `updated_at` first. Terminal directions collapse to one line per
   model-side status with an expansion:
   `… 42 tried · get(kind='quest', id='qu202467', extent='fisheye+1hop', q='contains:tried')`.
3. `_neighbor_label` prefixes every todo with its model-side status
   (`[active]`, `[tried]`, …) and a strand with its child count (`+13`), on
   every fisheye, not only quests.
4. A todo focus renders its `parent_id` parent and children.
5. `search(kind='todo', q=…, args={'under': '<quest handle>'})` scopes to
   the quest's direction tree, as memory `under=` scopes to a hub.
6. After step 6, evidence sits on directions; a direction focus shows it
   under Roadmap `served-by`, ordered by the existing
   `RING_GROUP_KIND_RANK`.

`view='directions'` returns the bounded open + do-not-repropose lists the
tick prompt reads (12 000-char budget, `dossier._OPEN_LEDGER_BUDGET_CHARS_DEFAULT`
moved to `directions`, `+N more` lines).

**Pinned by** `tests/test_quest_navigation.py` on a fixture shaped like
qu202467 (200 roots, 75 strands, 118 live): the quest fisheye lists every
live root with its status, strands first, terminal roots as count lines,
≤ 5,000 tokens (chars / 4); `q='contains:tried'` lists exactly the tried
roots; a strand focus lists its `parent_id` children; `under=` search
returns only tree members. Existing memory-hub fisheye tests stay green
unchanged.

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

**New:** `quest/directions.py` (slice 2), `quest/key_ideas.py` (slice 6),
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
| `quest/tick.py` | `build_tick_prompt` reads narrative, ledger forest, dialectic; `_apply_narrative_gate`; apply stage runs `ledger_ops`→`add_attempt`/`mark_attempt`, `append_ledger_entry`, `dialectic_ops`; finish stage `update_frontier_tree`, `rewrite_dossier` | prompt reads the newest synthesis entry + `directions` open/do-not-repropose lists; `direction_ops` (add/mark by `td` handle or quoted text; `nest` when > 40 live childless roots) → `directions`; `dossier_text` replaced by one synthesis entry (Q1); finish drops both dossier writes; apply and finish re-read the flag |
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
| `utils/eye_render.py`, `utils/refeye.py` | hub listing only for memory `section:index`; no todo status in labels; 8-per-label cap | §Quest navigation items 1–4 |
| `workers/job_types/draft_refresh.py` | attempt label into the owning quest's ledger | logbook `observation` entry; no direction (an LLM failure is not a ruled-out direction) |
| `precis_web/routes/refs.py` | quest page: dossier link, narrative, ledger, `paper_ref_id`; pathway page dossier link | direction tree (fisheye order), newest synthesis entry, frontier tree; pathway links the quest page |
| `templates/refs/quest_detail.html.j2`, `pathway_detail.html.j2` | Dossier column, "Open dossier (draft · fisheye)" | Directions + Synthesis + Frontier tree sections |
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
`test_relation_constraints.py`; the rest are incidental strings. A slice that
deletes a test moves its assertion into the replacement test in the same
commit (no loosening; Reto rules on any test that has no replacement other
than the weave tests above).

**Skills:** rewrite `precis-quest-help` (dossier, ledger, `view='dossier'`
sections → directions, navigation recipe: quest fisheye → strand →
evidence, `under=` search), `precis-quest-writing-help`, `precis-relations`
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
| 1 | Spike on the dev DB (`scripts/dev`): mint qu202467's and qu164903's forests as todos from a prod dump of their ledger chunks; measure the §Quest navigation paths with real titles; reconcile the qu164903 root count | S | Sonnet | a table of calls/tokens per path appended to this file; the 300-row hub cap and the 40-root `nest` threshold confirmed or replaced by measured values |
| 2 | `quest/directions.py` (todo-backed list/add/mark/nest, near-dup upsert, do-not-repropose + open renders, both status maps, `quest_evidence`; text logic moved from `dossier.py`, which re-imports it); rotation fence | M | Sonnet | `test_quest_directions.py` ports every `test_dossier_hygiene.py` assertion onto todos (Rh/Ru veto, prefix match ≥ 40 chars, never-regress merge, ancestor shadow) and pins the model-side map (`blocked`/`paused` on the open list); `test_todo_views` case: a direction todo is absent from `doable`/`queue`/nursery; `test_part_of_todo_to_quest_passes_constraints` |
| 3 | Navigation layer (§Quest navigation 1–6) and `view='directions'`/`'frontier'` | M | Opus | `tests/test_quest_navigation.py` as pinned above; memory-hub fisheye tests unchanged |
| 4 | Tick on the flag: quests with `meta.dossier_retired` and quests with no dossier use directions + synthesis entry + `direction_ops` (incl. `nest`); apply and finish re-read the flag; `ensure_dossier` refuses (raises `DossierRetired`) when the quest is flagged **or has no dossier**, so from this slice on it returns existing dossiers and creates none; each pre-slice-8 caller (`roadmap_tick`, `draft_refresh` via `add_attempt`/`mark_attempt`; `rulings` only reads via `dossier_ref_id` and already no-ops without a dossier) catches the refusal and skips its dossier write with a log line | L | Opus | Synthesis entry = `entry_type='reflection'` + `meta.synthesis=true`, WORM, ≤ `target_words`, gated by `narrative_growth_gate` against the previous synthesis entry; archive marker = `entry_type='note'` + `meta.archived_narrative=true`; no `ENTRY_TYPES` change, because the vocabulary is listed to the model in the tick prompt and accepted by the handler's `entry=`, model entries carry no meta (`_stage_apply` calls `append_entry` without `extra_meta`) so a model `reflection` never reads as a synthesis, and an added `synthesis` type would invite writes that `clamp_entry_type` would otherwise turn into `note`. Tests in `test_quest_tick.py`: flagged fixture runs llm→finish with no `dossier-of` link created and one synthesis entry; the prompt picks the synthesis entry over a newer plain `reflection`; `test_prompt_context_sections_bounded` (synthesis + directions sections ≤ `target_words` words + 12 000 chars on a 500-direction fixture); `nest` moves a direction under a strand and opens an umbrella only with ≥ 3 members; `test_no_dossier_resurrection` — every `ensure_dossier` path (`update_frontier_tree`, `ensure_ledger_chunk` and its readers/writers, `rewrite_dossier`, `ensure_dialectic_chunk`, `roadmap_tick`, `draft_refresh`) on a flagged quest and on a dossier-less quest creates nothing, plus a tick checkpointed at `tick:apply` and one at `tick:finish` on an unflagged quest, flag flipped, resumed: no dossier write, its ledger ops present as todos, its `dossier_text` as the synthesis entry; unflagged-with-dossier fixtures unchanged |
| 5 | `retire-dossier` tool: dry run, apply (steps 1–7), `--undo`, `--rehome`, in-flight check, step-0 stamp check; `serves`-set readers switched to `directions.quest_evidence` | M | Sonnet | `test_retire_dossier.py`: lossless round trip (forest == todo tree, statuses by the table), idempotent re-run, archived entry excluded from `_logbook_tail`, `test_serving_draft_untouched`, `test_apply_undo_apply_round_trip`, re-home by rule a, b and c, deepest-on-path, disjoint hits stay on the quest and print as ambiguous, refuses while the job is `running` or at `tick:*`, refuses a quest with narrative and no `key_ideas_extracted_at`; reader tests: quest evidence counts equal before and after a re-home |
| 6 | Step 0: `extract-key-ideas` (prompt, verbatim-span check, restated-hub dedupe, cites, idempotency, stamp) | M | Sonnet build; Opus reviews the prompt on 3 real dossiers' dry runs | `test_key_ideas.py` with a stubbed LLM: a claim without a verbatim span is dropped and listed; a restated claim links the existing finding; cites resolve to `cites` edges; re-run writes nothing; stamp set |
| 7 | Weave delete + `hub_refine` quest seed: move `QUEST_BODY_*` to `body.py`, delete the seven modules, their CLI command, routes and tests; add the seed | M | Sonnet | `rtk proxy grep -rln "weave_tick\|residual_cluster\|citation_mint" src` empty; `tests/workers/test_hub_refine.py` case: a quest-serving paper with no inbound evidence edge is selected, one already placed is not, the pass bound holds |
| 8 | Remaining consumers: web quest page, rulings, roadmap capability ledger, `draft_refresh`, compute reset | L (split a–d by module) | Sonnet | each module's rewritten tests; `test_quest_dashboard.py`: no dossier link, direction tree and frontier tree present, link from gr510505 fixed; `test_refresh_on_retired_quest_logs_and_mints_no_dossier` |
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
- **Wide fisheye on every open.** (a) lists every live root: ~3.5k tokens on
  qu202467. Mitigation: terminal roots collapse to count lines; `nest`
  keeps childless roots ≤ 40 live; the 300-row hub cap bounds the worst case.
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

## Decisions

"Knowledge graph" here means the refs and their typed links: papers,
findings, measures, todos, quests, memories. All folded into the body above.

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

## Open questions for Reto

1. **Tick-authored umbrella strands.** When a quest has > 40 live childless
   roots (qu202467: 85, qu164903: 108), may the tick's `nest` op open an
   umbrella strand todo to group them? Rec: yes — it is the only index
   structure that is not synthetic, the tick already owns directions, and
   it shortens the quest fisheye from ~3.5k toward ~1.5k tokens.
   Alternative: no `nest`; the flat status-tagged list (a) carries
   navigation alone.

## Links

- gr510505 (ruling + the shape proposed in `chat-99`) ·
  `quest-graph-as-dossier.md` (2026-10-01 ruling) ·
  `paper-writing-pipeline.md` (export = writer step; embed target for
  `quest-artifacts-in-dossier.md`), `draft-refresh.md` (narrative gate
  reuse) · skill `precis-fisheye-help` (fisheye ring groups).
