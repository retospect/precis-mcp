"""Graph garden — deterministic upkeep signals, LLM-judged proposals.

The knowledge graph grows without consolidation: duplicates accumulate,
conflated terms stay conflated, wrong edges persist. The gardener is the
maintenance pass that finds them — and stops there.

**Proposals only.** Nothing in this package merges, splits, prunes,
retags or relinks the graph on its own. Detectors emit *candidates*;
a proposer turns candidates into *proposals*; every proposal lands as
one ``reviews`` row (migration 0185) with ``verdict='proposed'``,
``actor='graph-garden'`` and a JSON payload in ``note`` (action,
evidence, reversal hints) for a human or frontier-model judge. Apply
is a separate explicit verb — :mod:`precis.graph.garden.apply`, reached
through ``precis garden apply`` or the consumer (step 5), never by a
detector or the proposer. A pass over
a clean graph writes nothing: no reviews, no chunks, no log spam.

**Pipeline** (:mod:`precis.graph.garden.runner`):

1. Detectors (:mod:`precis.graph.garden.candidates`) — deterministic
   SQL/Python over the live graph. No LLM, no writes:
   ``dup_pair`` (card embedding + name/alias similarity, dimension-gated
   for taxa; memories compare body embedding + body hash),
   ``paper_identity`` (live papers sharing a normalized title, gated on
   DOI/year/first author), ``orphan`` (taxon reaching no start node),
   ``stale`` (untouched, unlinked, unviewed node), ``dangling_edge``
   (a live ref's non-provenance link to a retired one) / ``cycle_edge``
   (a 2-cycle in the taxon DAG) /
   ``redundant_link`` (a machine ``related-to`` edge whose endpoints are
   mutual top-k card neighbours inside the recall floor — the edge
   restates the index; proposal only, apply stays merge-only),
   ``untagged`` (a ref missing a namespace its kind already uses),
   ``polysemy`` (split signals), ``sibling_confusion`` (exclusive-axis
   siblings one classification run can't tell apart).
2. Proposer (:mod:`precis.graph.garden.proposer`) — one routed LLM call
   per candidate renders it into a structured verdict
   (``accept``/rationale). Model placement is configuration: the call
   carries ``source='graph_garden:propose'`` (an ``llm.op.*`` row steers
   it; see ``precis.utils.llm.operations``). Nothing is pinned to a
   host or model name in code.
3. Persistence (:mod:`precis.graph.garden.proposals`) — accepted
   verdicts become ``reviews`` rows, deduplicated against pending
   proposals with the same proposal key and against a rejection of the
   target's unchanged content. A proposer rejection is a ``rejected``
   row too, so an unchanged candidate is asked once per prompt version,
   not every run (gr506046). The tick's ``max_proposals`` is shared
   round-robin across the detectors that produced candidates.
4. The sampled reviewer (:mod:`precis.graph.garden.reviewer`,
   ``precis garden review``) — a FRONTIER-tier burn-down over open
   proposals older than a few days: one approve/reject verdict per
   sampled proposal, recorded as a NON-TERMINAL ``garden-reviewer``
   row (the proposal stays open for a human; apply doors exist for
   merge and relink). The per-detector agreement rate is the proposer's
   quality bar — ``precis garden stats``, reporting only.
5. The consumer (:mod:`precis.graph.garden.consumer`, gr506044, Reto's
   2026-10-23 ruling) — the one place a pass applies anything. Run by
   the job after each tick: dangling-edge prunes apply; Opus approves
   or rejects (terminal row with a reason code) pending merge/relink
   proposals, and an approve applies through the undoable door
   (merge repointing, or the reviewed structural write for a relink —
   gr507641).

**Scheduling.** The ``graph_garden`` job_type (``job_inproc`` executor)
runs one bounded pass per tick — ``params.pass`` picks the detector
subset (``merge|split|relink|prune|tag-sweep|siblings|paper-identity|all``,
or a list), ``limit`` bounds candidates scanned, ``max_proposals``
bounds writes, and each detector runs under a wall-clock deadline that
skips it rather than zeroing the tick (gr503580). The wide scans walk
each kind from a cursor saved on the job's ``meta.garden.cursors``
(:func:`precis.graph.garden.service.saved_cursors`), so successive runs
cover the corpus. The cadence is an operator-seeded
**recurring todo** under Watches — ``meta.schedule`` +
``meta.executor='job_inproc'`` + ``meta.job_type='graph_garden'`` (the
job-mode automation shape, ``precis-recurring-help``): each due tick
spawns a subtask the dispatch pass mints the job from, and the
spawner's ``spawned_for_tick`` idempotency plus collision-skip on a
still-open previous child are the run bound. The gates are evaluated
per run by the job dispatcher
(:mod:`precis.workers.job_types.graph_garden`): a dark
``graph_garden`` service_config row
(:func:`precis.graph.garden.service.graph_garden_enabled`) or an
active cooling streak writes ``meta.garden.skipped`` and exits. After
``DRY_RUNS_ESCALATE`` consecutive empty runs the pass cools for
``COOL_RUNS`` runs instead of scanning every tick.

**Polysemy contract (provisional).** The split pass fires when a live
``finding`` linked to a node carries ``meta.annotation='polysemy'``
(the uses list lives in ``meta.uses``) — nothing mints those findings
yet (the backlog's open question), so the detector also flags a node
reaching *two different start nodes* as a disjoint-cluster hint.
Do not treat the hint as evidence: a proposal needs the finding, or a
judge's explicit accept on the hint.

**Sibling-confusion contract.** ``instance-of`` edges may carry
``meta.run`` (a classification-run id). Two siblings on an exclusive
axis whose per-run labels swap — or that share classified instances —
are a merge/sharpen/re-axis candidate. "Exclusive axis" is a param
(``params.exclusive_axes``) until the taxonomy lands axis declarations;
the 2026-10-05 ruling stands: similar labels on disjoint siblings are
*not* a merge signal on their own.
"""

from __future__ import annotations
