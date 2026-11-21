"""Graph garden — deterministic upkeep signals, LLM-judged proposals.

Detectors find duplicate nodes, conflated terms and wrong edges without
mutating the graph.

**Proposals only.** Detectors emit candidates; the proposer emits proposals:
one ``reviews`` row (migration 0185), ``verdict='proposed'``,
``actor='graph-garden'``, JSON ``note`` (action, evidence, reversal hints)
for a human or frontier-model judge. Merging, splitting, pruning, retagging
and relinking require explicit apply — :mod:`precis.graph.garden.apply`,
via ``precis garden apply`` or the consumer (step 5), never detectors or
proposer. Clean passes write nothing; repeated timeouts alert for starvation
(:mod:`.service`).

Memory proposals pin supporting refs and edited bodies; unpinned legacy
proposals close before paid review. Stale evidence closes with retryable rejection,
allowing fresh review despite unchanged target content. Fresh detector actions
must match before paid review and apply: supersede cannot degrade to retire.
Deterministic refusals close the key; unexpected deterministic apply failures
hold it, preventing repeated paid review. Operational DB failures hold after five
recorded attempts. Memory approvals persist separately before apply, survive
rollback, and serve bounded later-tick retries with identical evidence checks.
Failed approval persistence prevents apply; later ticks may pay again, at most
once per proposal per tick within the shared review limit. Failed hold writes
count as errors without aborting the tick. CLI shows hold reasons; rejecting an
apply-error hold reopens its key. Unresolved ops wait one day; resolved ops get
fresh proposals. Applied find/span pairs must match fresh detector ops; link
removals refuse, preventing model overrides from expanding detector edit scope.
Notation review pins one authored token per memory to bound edits.
Undo terminally rejects restored content, preventing next-tick reapplication.
Supersede and retire snapshot links for undo, refusing changed endpoints.
Pending reads mark tombstones stale and show any successor. With memory apply
enabled, the consumer locks and rechecks the target before recording a terminal
``target_gone`` rejection. Restored targets await the next scan.
Retirement validates successor evidence before compaction so stale proposals close retryably.
Supersede checks incoming and outgoing part-of migrations
before writing and refuses edges that close a containment cycle. A direct
parent remains a valid successor: migration drops the collapsed self-loop.
Memory rewrite remaps changed header anchors and undo restores their saved
chunk identities because replacing a
card cascades incoming and outgoing links. The reversal snapshot includes
incoming header links; an unrestorable chunk anchor refuses the transaction.
Undo retains unchanged links, including legacy version-zero edges that strict
trust mode cannot recreate exactly. Replacements of those legacy edges refuse
before writing; unknown-origin stamped links use a create-time import act.
Review stamps keep token-safe model names readable, encoding others as
``hex-`` plus UTF-8 hex (even names starting with that prefix), so
namespaced model IDs survive the fixed stamp grammar; review rows and
trust acts retain the original model string.

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
   The ``memory`` pass adds ``dangling_mention`` (a handle naming a
   retired ref), ``grab_bag`` (one memory carrying many independent
   claims — a split candidate), ``thread_shape`` (a thread ``Left:``
   missing or done, or a Left-line sha confirmed on ``origin/prod``),
   ``anchor_rot`` (a cited file/symbol, path, section or sha anchor no
   longer resolving on committed ``main``), ``compact`` (expired,
   half-history or retire-pending, plus cold alone on decaying shapes
   — owes a per-sentence keep/move/drop plan;
   :func:`precis.graph.garden.apply.apply_compact`) and ``griped``
   (open ``project:memory`` complaints, newest first). The opt-in
   ``memory-review`` pass is proposal-only — no apply door — and adds
   ``review_due`` (expired, unreviewed, sha- or version-moved, or aged:
   30 days for reference/gotchas, 60 for runbooks, ``meta.review_by``
   for workflow/rulings, threads never; expiry shapes give threads and
   snapshots 14 days, incidents/campaigns 90), ``restated_claim`` (a
   sentence of eight or more words restating a hub sibling's claim; the
   keeper ranks reference, gotchas, threads, then the lower ref id),
   ``title_shape``, ``negative_undated``, ``notation``,
   ``source_drift`` and ``entity_claim_stale``; the one-off
   ``host-facts`` pass flags census-machine state claims with no date
   or machine handle. Per-detector thresholds, caps, sha/key pins and
   evidence shapes: :mod:`precis.graph.garden.candidates`.
2. Proposer (:mod:`precis.graph.garden.proposer`) — one routed LLM call
   per candidate returns structured ``accept``/rationale. Memory detectors'
   body candidates receive fisheye+1hop and verbatim
   ``precis-fix-memory-gripe-help`` checklist; one-action JSON answers:
   rewrite / qualify / supersede / retire / no-change / ask (gr560762).
   ``source='graph_garden:propose'`` routes through configured ``llm.op.*``
   (``precis.utils.llm.operations``), never a code-pinned host/model.
3. Persistence (:mod:`precis.graph.garden.proposals`) — accepted verdicts
   become ``reviews`` rows, deduplicated by pending proposal key and
   rejected unchanged target content. Proposer refusals also persist as
   ``rejected``: unchanged candidates are asked once per prompt version
   (gr506046). Candidate-producing detectors share ``max_proposals``
   round-robin; ``griped`` candidates drain first (gr657257).
4. Sampled review (:mod:`precis.graph.garden.reviewer`, ``precis garden review``)
   uses FRONTIER to judge older pending proposals. Its nonterminal
   ``garden-reviewer`` verdicts measure per-detector agreement in
   ``precis garden stats``; humans still decide those proposals.
5. The consumer (:mod:`precis.graph.garden.consumer`, gr506044) applies
   dangling-edge prunes after each tick. Opus rejections record terminal
   reason codes; merge and relink approvals use undoable apply doors (gr507641).
   With ``PRECIS_GARDEN_MEMORY_APPLY`` enabled, memory rewrite/supersede/retire/split
   proposals from ``dangling_mention``, ``anchor_rot``, ``grab_bag``,
   ``griped`` and ``thread_shape`` share this review budget. Eligibility and
   apply-time re-checks use one detector registry so proposals that cannot
   apply do not spend a review. A ``dangling_mention`` proposal whose ops
   are all bare swaps onto verified duplicate successors applies without a
   call (``garden.applied='mention'``, before/after spans recorded).
   ``md:``/``python:`` successors refuse (their content cannot be
   shown or pinned); human-origin and currently ``considered`` targets
   are held for a human.
   Humans review ``memory-review`` proposals and ``qualify`` verdicts.
   An ``ask`` files a detector-tiered review todo unless a done review
   matches the memory, detector and content SHA across proposal retries.
   A ``griped`` apply or no-change with no remaining sibling fix
   replays the route and hub recall set before closing the gripe
   with ``resolved:`` (gr561873).

SHA-pinned grounding audits verify passages and inference warrants.
Exact quotes bind local premises. Each premise needs its own support
judgment. See :mod:`precis.graph.garden.grounding` for budgets and repairs.
Unresolved checks get one bounded handoff todo; returned candidate
evidence only reopens re-audit.

**Memory reconciliation.** Memory candidates (including ``dup_pair``)
and interactive ``precis-make-memory`` share packaged memory-help
policy (:mod:`precis.utils.memory_reconcile`) across proposer, consumer
and sampled reviewers; a hand-bumped version gates only
memory proposer rejections, keeping identity/conflict/preservation
judgments consistent without detector interpretation or broader apply
doors. Sampled reviews read bounded body excerpts; merge repointing
never synthesizes loser's unique claims into the winner.

**Scheduling.** ``graph_garden`` job_type (``job_inproc`` executor) runs one
bounded pass per tick. ``params.pass`` selects detectors
(``merge|split|relink|prune|tag-sweep|siblings|paper-identity|memory|all`` or
list); ``limit`` bounds scans, ``max_proposals`` bounds writes. Per-detector
wall-clock deadlines skip that detector, preserving the tick (gr503580).
Wide scans resume per-kind cursors in job ``meta.garden.cursors``
(:func:`precis.graph.garden.service.saved_cursors`), covering the corpus
across runs. An operator seeds a Watches
**recurring todo** (``meta.schedule`` + ``meta.executor='job_inproc'``
+ ``meta.job_type='graph_garden'``, ``precis-recurring-help``); each
due tick spawns a subtask dispatch mints its job from.
Spawner ``spawned_for_tick`` idempotency and collision-skipping an open
previous child bound runs. Per-run dispatcher gates
(:mod:`precis.workers.job_types.graph_garden`): dark ``graph_garden``
service_config (:func:`precis.graph.garden.service.graph_garden_enabled`)
or active cooling writes ``meta.garden.skipped`` and exits.
``DRY_RUNS_ESCALATE`` consecutive empty runs trigger ``COOL_RUNS``
cooling runs without scans.

**Polysemy contract (provisional).** The split pass fires when a live
``finding`` linked to a node carries ``meta.annotation='polysemy'``
(the uses list lives in ``meta.uses``) — nothing mints those findings
yet (the backlog's open question), so the detector also flags a node
reaching *two different start nodes* as a disjoint-cluster hint.
The hint is not evidence: a proposal needs the finding, or a judge's
explicit accept on the hint.

**Sibling-confusion contract.** ``instance-of`` edges may carry
``meta.run`` (a classification-run id); two exclusive-axis siblings
whose per-run labels swap — or that share classified instances — are a
merge/sharpen/re-axis candidate. "Exclusive axis" is a param
(``params.exclusive_axes``) until axis declarations land; the 2026-10-05
ruling stands: similar labels on disjoint siblings are *not* a merge
signal.
"""

from __future__ import annotations
