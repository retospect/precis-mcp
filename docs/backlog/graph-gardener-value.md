---
status: draft
pillar: memory-graph
title: Gardener value — a link is written only when retrieval cannot recompute it, every machine edge carries a fixed annotation, and a per-quest retrieval eval decides whether a pass stays on
prio: high
model: opus
---

# Gardener value: write only what RAG cannot recompute, and measure it

Reto, 2026-10-21: "If a fisheye finds things already with a RAG call, will
a specific link actually help? What is the value add of the graph
gardener, have we done this right?" Then: run the gardener on castor
(openai/gpt-oss-120b) for a few days, make the changes below, and find
what else is worth doing. Builds on [graph-gardener](graph-gardener.md)
(v1 shipped, lit on prod 2026-10-21: `service_config` melchior
`graph_garden` prio 1, proposer routed to castor by the
`llm.op.graph_garden:propose` local chain),
[knowledge-mesh](knowledge-mesh.md) §4 (the per-quest mesher) and
[local-mesh-upkeep](local-mesh-upkeep.md) (per-action bars, reviews
ledger).

## The argument (binding)

A stored edge adds value only when it holds something retrieval cannot
recompute on demand:

1. **Stance** — `supports`, `disputes`, `explains`, `qualifies`. Two
   papers that disagree on a barrier embed almost identically; similarity
   cannot tell agreement from contradiction.
2. **Distant connection** — a mechanism in one field that explains a
   result in another usually sits below the retrieval cutoff, so RAG never
   co-retrieves the pair.
3. **A settled judgment** — who checked the edge, against which content,
   when. Without it every agent re-derives the same judgment from raw
   chunks, in tokens, possibly differently.
4. **Provenance** — `derived-from`, `establishes`: not inferable later.
5. **Identity** — merging duplicates removes retrieval noise; this is
   most of what gardener v1 does.

A `related-to` edge between embedding neighbours adds nothing: it
duplicates the index and makes the fisheye (which renders links) noisier.

## Slices

1. ~~Machine-edge write rule + annotation.~~ slice 1 landed at
   9aae68dea (worktree `gardener-s1`): the gate lives in
   `store/_links_ops.py::add_link` (`_machine_edge_admission`); a machine
   writer is a `generated`/`computed` trust context carrying a model
   identity — human and agent-without-model writes are untouched.
   `related-to`/`see-also` refused; structural rels refused with a
   gardener-ledger `next=`; other rels need `meta.evidence` chunk pins +
   `meta.{model,prompt_version,run_id,why}`; non-stance rels need
   endpoints past `eye_render.RECALL_MAX_DISTANCE`; `explains` hub→hub
   routes through `taproot.hub.link_claims` (meta param + the
   `claim_link_door` re-entry marker). Merge repointing is exempt.
2. ~~Retrieval eval (the value measure).~~ slice 2 landed at d1099d8b8
   (`precis mesh retrieval` / `retrieval-compare`,
   `mesh_eval/retrieval.py`); gold labels await Reto's review — builder:
   `uv run python scripts/llm_eval/build_retrieval_gold.py --prod --quest
   qu500066 --endpoint-url <url> --model <id>`.
3. **Gardener v1 corrections** — slice 3 landed (worktree
   `gardener-s3`, commits 7a9cfe309/9d623d044/9ad06c3d3/d2ec4e443):
   relink keeps only orphans past `RELINK_MIN_DISTANCE` (the
   `+recall` floor) or with no card vector; the `prune` pass gained
   `redundant_link` (machine `related-to` whose endpoints are mutual
   top-5 inside `PRUNE_MAX_DISTANCE`); `precis garden review|stats`
   runs the Opus sample (20/day, >3d) plus a Fable re-review of a
   sample of the same proposals — both non-terminal — and reports
   per-detector proposer–Opus and Opus–Fable agreement side by side.
   Merge stays the only auto-apply path.
4. **Per-quest scout on castor** (knowledge-mesh §4 made concrete): one
   tick per active quest reads `fisheye+2hop` around the quest, picks the
   thinnest rubric axis, and does one of: widen (paper search), read
   (queue the reader on held papers for that axis), stance (for chunk
   pairs on the axis that are near in the index but state different
   numbers → a `disputes`/`supports` proposal), bridge (for a held
   finding, search other quests' chunks below the cutoff for a mechanism
   that explains it → an `explains` proposal), finding (mint a `finding`
   from a chunk with `establishes` + measure). All writes pass slice 1;
   findings and stance edges stay proposals until the reviewer bar is
   met.
5. **Castor filler: extract → mesh** (Reto 2026-10-21: "we want to see
   what the quality of castor output can be, and then fix things until it
   is adequate"; castor stays the gardener's default model). The
   gardener stops being a once-a-day job and becomes castor's idle-time
   filler: a small job (one paper or one candidate set) is minted only
   while castor runs fewer than ~8 of its 32 requests, at the lowest
   priority, yielding to interactive calls. Work, in order: (a)
   `survey_extract` on full-text papers serving active quests (measures
   + findings, accepted per batch by `measure_accept`'s sampled Wilson
   bar, never item by item); (b) deterministic numeric mesh over the new
   measures (same measurand, reference, normalization and conditions:
   compatible values → `corroborates`, incompatible → `disputes`; castor
   writes only the `why`); (c) findings grouped into claim hubs; (d)
   `explains` between claim hubs (claim-hub-only per migration 0203 and
   `taproot.hub.link_claims`: accepted 2026-10-21, chunk-level
   explanation is the hub's evidence, not the edge's); (e) the v1
   detectors. Direction: the work list ranks by the active quests'
   priority, then the rubric questions slice 2's eval cannot answer
   (other-pass 5), then new full-text papers; a pass whose sampled
   acceptance or eval delta falls under the bar is throttled.
   **Trial first, before any scheduler code:** ~100 `survey_extract`
   jobs on castor over held full-text papers of active quests, then an
   Opus read of the output (sampled measures against their chunks,
   failure classes: wrong number, wrong measurand, missing condition,
   unit, hallucinated anchor, JSON), fix what it finds, repeat until the
   acceptance bar holds; only then the idle gate and the mesh stages.

## Other passes worth having (ranked, not yet sliced)

1. **Numeric conflict → dispute** (knowledge-mesh §3): the same
   measurand stated with incompatible values in two chunks is the
   highest-value stance edge and is mostly deterministic (unit-normalised
   comparison); the model only writes the `why`.
2. **Hub summaries per cluster the scout keeps landing in**, linked
   `derived-from`, so a quest's fisheye shows a handful of summaries
   instead of 181 papers (quest-graph-as-dossier). Measured by slice 2's
   token score.
3. **Paper identity**: duplicate paper refs (preprint vs journal, stub vs
   held) merged through the gardener's merge path; directly cuts
   retrieval noise.
4. **Fisheye ranking, not more edges**: render evidential and reviewed
   edges first, collapse unreviewed generated ones to a count. Cheaper
   than pruning and immediately visible.
5. **Question-driven gaps**: from each quest's rubric, list the questions
   slice 2's eval cannot answer from the graph; those become the scout's
   widen/read targets instead of the thinnest-axis heuristic.

## Watch (running now)

A 6-hourly check in the chat desk session reads gardener runs, proposals
and `graph_garden:propose` call stats, and files gripes per class: model
confusion, skill confusion, API, flow, token efficiency. Findings feed
slice 3.

## Explicitly NOT in scope

Auto-applying anything but merge; new relation types; the paper
full-text acquisition problem (someone else's); cloud fallback for the
castor chain (failures should be visible while castor is on trial).

## Target + blast radius

`src/precis/links` write path (the gate), `src/precis/graph/garden/`
(detectors, reviewer pass), `src/precis/llm_eval/` (retrieval task),
a new scout pass beside the gardener service. Skills:
`precis-gardener-help`, `precis-relations` (machine-edge rule),
`precis-link-help`.

## Open questions (Reto)

- Q2 Reviewer tier for the burn-down: Opus sample of 20/day, or Fable?

Ready-check 2026-10-21 (coordinator; all three slices needs-work; no
migration in any slice; slices 2 and 3 share no code, slice 3 owns
`precis-gardener-help.md`). Also removed four stray tool-call lines
that had landed at the end of this file.

- Slice 3 blocker: "taxa whose nearest start node the index already ranks
  first" is undefined. `garden/candidates.py::orphan_taxon_candidates`
  already takes a plain cosine min over card vectors, so the literal
  reading drops every orphan with a vector, and "reaches nothing" is
  vacuous (every candidate has `reaches_no_start=True`). Proposed: keep a
  candidate when `candidate_parent_distance` > a named constant (sibling
  of `DUP_MAX_DISTANCE`) or no card vector exists; tests on both sides.
- Slice 3 blocker: prune-redundant has no k, distance bound, similarity
  source or SQL for "machine related-to". Proposed: card vectors as the
  other detectors, k = 5, a named distance constant; machine =
  `relation='related-to' AND links.trust_origin IN ('generated','computed')`;
  honour `scan_cap`/`limit` (56,305 related-to rows on prod). Wire into
  `_DETECTOR_BY_PASS`/`PASS_CHOICES` as `prune`, candidate
  `action='prune'`, `payload={'link_id': …}`; proposal row only
  (`apply.py` applies merges only).
- Slice 3 blocker: the reviewer burn-down has no module, entry point,
  sampling rule, actor, agreement query or surface, and a reviewer
  `approved` via `resolve_proposal` would read as applied for a merge.
  Proposed: `graph/garden/reviewer.py`, actor `garden-reviewer`, payload
  `reviewed_by_model`/`sampled: true` (not terminal), age > 3 d, 20/day
  global, agreement = reviewer-approve share per detector, surfaced in
  `precis garden stats`; reports only, no gate (local-mesh-upkeep has no
  number yet). Needs Q2.
- Slice 3 blocker: no acceptance tests. Proposed: narrowing both sides;
  prune fixture (mutual pair proposed, non-mutual not, human-authored
  not, key deduped); reviewer with a fake model callable (age, cap,
  non-terminal verdict); `tests/test_garden.py` unchanged.
- Slice 2 blocker: `precis.llm_eval` scores a model per capability axis
  (`harness.py::run_axis`, `GoldTask.axis` ∈ `CAPABILITY_AXES`); retrieval
  has no model call. Proposed: a standalone runner after the
  `src/precis/mesh_eval/` pattern (JSONL sets, Wilson CI), no
  `record_eval`.
- Slice 2 blocker: gold set unspecified. Proposed: fixture
  `{question, quest, gold_chunks:[pc ids]}`, builder
  `scripts/llm_eval/build_retrieval_gold.py` over `_prod_ro.py`, labels
  drafted by a model and reviewed by Reto, gitignored under
  `scripts/llm_eval/gold_set/local/`; tests on a synthetic fixture,
  skipped pilot when the file is absent; never the session MCP.
- Slice 2 blocker: metrics undefined. Proposed: recall@10 and
  tokens-to-first-gold (cumulative rendered output, chars/4, cap =
  not reached); the fisheye arm anchors at the quest handle; before/after
  as dated reports on a snapshot; a pass stays on if recall@10 rises
  ≥ 0.05 or tokens fall ≥ 10 % with no recall loss. The scout arm cannot
  run (no scout in code); keep the gardener arm only.
- Slice 1: buildable on landed trust slice 1 + 2a (`store/_links_ops.py::
  add_link` choke point, `trust_door`); does not need 2b. Blockers:
  Q1; "machine writer" needs a test (`trust_origin` in
  generated/computed from the write context); grounding pins live in
  `links.meta.evidence` until 2b (plus `meta.model`, `prompt_version`,
  `run_id`, `why`; not `meta.origin`); the `+recall` cutoff is
  `utils/eye_render.py::_recall`'s chunk-level `_RECALL_K` + distance
  floor, name it; enumerate the stance/provenance rels and check each is
  registered (`explains` is not in the prod inventory); exempt
  `apply_merge`/`undo_merge` repointing; no machine writer exists yet
  (mesher/scout unbuilt); skills `precis-relations`, `precis-link-help`,
  `precis-gardener-help`; refusals carry `next=`.

Rulings 2026-10-21 (Reto, via chat; resolve the findings above):

- Slices 3 and 2: the proposed resolutions are accepted as written
  (relink distance constant, prune-redundant on card vectors with mutual
  top-5 over machine `related-to`, standalone retrieval runner, gold set
  `{question, quest, gold_chunks}`, recall@10 + tokens-to-first-gold,
  keep threshold +0.05 recall or −10 % tokens without recall loss).
- Reviewer: Opus, 20/day. A Fable re-review of a sample of the same
  proposals runs beside it; `precis garden stats` reports, per detector,
  castor-proposer–Opus agreement and Opus–Fable agreement side by side.
  Both stay non-terminal reviews; neither gates anything.
- Gold labels: model-drafted, then Reto reviews qu500066's three
  questions; the review does not block the runner.
- Slice 1: one door, the trust-state link admission (`Store.add_link`).
  Stance/provenance rels allowed to machine writers with an evidence
  chunk: `supports`, `disputes`, `qualifies`, `derived-from`,
  `establishes`, `corroborates`, `refines`, `explains`. All are already
  registered on prod (checked 2026-10-21; `explains` is migration 0203,
  so no migration is needed). Caveat: `explains` is claim-hub → claim-hub
  only and is not an evidence edge (0203; enforced at
  `taproot.hub.link_claims`), so slice 1 must route it through that
  door or refuse it outside claim hubs. Structural rels (`specialises`/
  `generalises`, `part-of`/`contains`, `instance-of`) are allowed to
  machine writers as gardener-ledger proposals only, never direct
  writes; they need no evidence chunk and stay generated/unreviewed until
  a recorded review.
