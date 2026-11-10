"""Graph garden — deterministic upkeep signals, LLM-judged proposals.

Detectors find duplicate nodes, conflated terms and wrong edges without
mutating the graph.

**Proposals only.** Detectors emit candidates; the proposer emits proposals:
one ``reviews`` row (migration 0185), ``verdict='proposed'``,
``actor='graph-garden'``, JSON ``note`` (action, evidence, reversal hints)
for a human or frontier-model judge. Merging, splitting, pruning, retagging
and relinking require explicit apply — :mod:`precis.graph.garden.apply`,
via ``precis garden apply`` or the consumer (step 5), never detectors or
proposer. A clean pass writes no reviews, chunks or log spam.

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
Undo terminally rejects restored content, preventing next-tick reapplication.
Supersede and retire snapshot links for undo, refusing changed endpoints.
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
Review stamps keep token-safe model names readable and encode other names as
``hex-`` plus UTF-8 hex (including names starting with that reserved prefix),
so namespaced model IDs retain their identity within the fixed stamp grammar.
Review rows and trust acts retain the original model string.

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
   The ``memory`` pass adds ``dangling_mention``: one handle scan over
   live memory bodies joined to retired refs, since mention edges only
   target live refs and disappear after retirement. ``grab_bag`` flags a
   memory carrying many independent claims — a multi-claim title smell,
   or >=4 top-level items whose sentence embeddings cluster into >=3
   groups — and the evaluator proposes atomic children pinned to the
   detector's sentence spans; the reviewed split door mints them, then
   retires the parent (tombstone carries ``meta.split_into``) or shrinks
   it to a <=3-line summary. Rewrite proposals pin
   the content SHA and spans, with tombstone/edge successors as evidence.
   Unresolved replacement text stays null and outside consumer review;
   DREAMs are excluded.
   Tombstone successors must resolve to a live ref within 16 cycle-checked
   hops, using the tombstone reader's resolver so converted kinds retain
   their redirect. Each memory carries at most 20 matches, marked when capped,
   to bound the judge's evidence. Memory detectors have their own action sets
   and prompt version so their rules cannot reopen existing v2 rejections.
   These rewrites can use the reviewed memory door behind the default-off
   ``PRECIS_GARDEN_MEMORY_APPLY`` consumer gate; a mention whose successor
   is a verified duplicate (merge-made redirect, or a same-title
   ``supersedes`` target — ``evidence.mentions[].duplicate_successor``)
   applies with no model call, recorded ``garden.applied='mention'``
   (gr564554).
   ``thread_shape`` covers tagged thread children too: missing ``Left:``,
   terminal todo/gripe work, or a Left-line commit sha confirmed on
   ``origin/prod`` proposes supersede by the nearest live ancestor with a
   current ``Left:``, else retire. A supersede proposal requires
   preserving needed figures in the ancestor's ``Earlier:`` line;
   retirement also requires compaction review. No edits occur at
   detection. Sha-on-prod verdicts come from ``git merge-base
   --is-ancestor`` in the configured main checkout; when the main root or
   the prod ref is unavailable every sha stays ``not_checked`` and no
   landed-work claim is made.
   ``compact`` (gr561025) fires on expiry, 90-day-cold reads, a
   half-history body or any pending retire proposal: the evaluator owes a
   per-sentence plan (keep / move-to related — hub family, qualifies,
   ``+recall`` neighbours, restated_claim matches / mint atomic / drop
   only as history or restatement) accounting for every detector
   sentence; ``apply_compact`` runs it as one unit with a single
   reversal record only through ``precis garden apply`` — the consumer
   reviews but never auto-applies it. ``undo_compact`` un-retires the
   shell, then restores every touched body, then retires the minted
   children. Both retirement doors refuse while a compact proposal is
   pending or while the detector fires on content no completed
   compaction covers — a rejected plan preserves nothing.
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
   round-robin.
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
   With ``PRECIS_GARDEN_MEMORY_APPLY`` enabled, memory rewrite/supersede/retire/split
   proposals from ``dangling_mention``, ``anchor_rot``, ``grab_bag``,
   ``griped`` and ``thread_shape`` share this review budget. Eligibility and
   apply-time re-checks use one detector registry so proposals that cannot
   apply do not spend a review. A ``dangling_mention`` proposal whose ops
   are all bare swaps onto verified duplicate successors applies without a
   call (``garden.applied='mention'``, before/after spans recorded).
   The six ``memory-review`` detectors stay
   pending without consumer review or state changes. ``qualify`` and ``ask``
   remain outside that consumer; an ``ask`` verdict files a ``memory-review``
   todo from the runner (tiered by smell class), never a proposal. A
   ``griped`` apply — and an evaluator no-change once no sibling fix
   path remains — replays the gripe's route and hub recall set and
   closes it with a ``resolved:`` comment once the deterministic pass
   holds (gr561873).

**Memory reconciliation.** Memory candidates (including ``dup_pair``) and
interactive ``precis-make-memory`` share packaged memory-help policy via
:mod:`precis.utils.memory_reconcile`. Proposer, consumer and sampled reviewers
use the identical section; its digest versions only memory proposer rejections.
Identity/conflict/preservation judgments stay consistent without detector
interpretation or broader apply doors. Sampled reviews read bounded body excerpts;
merge repointing never synthesizes loser's unique claims into the winner.

**Scheduling.** ``graph_garden`` job_type (``job_inproc`` executor) runs one
bounded pass per tick. ``params.pass`` selects detectors
(``merge|split|relink|prune|tag-sweep|siblings|paper-identity|memory|all`` or
list); ``limit`` bounds scans, ``max_proposals`` bounds writes. Per-detector
wall-clock deadlines skip that detector, preserving the tick (gr503580).
Wide scans resume per-kind cursors in job ``meta.garden.cursors``
(:func:`precis.graph.garden.service.saved_cursors`), covering the corpus
across runs. An operator seeds a Watches **recurring todo**:
``meta.schedule``, ``meta.executor='job_inproc'``,
``meta.job_type='graph_garden'`` (``precis-recurring-help`` job-mode
automation). Each due tick spawns a subtask; dispatch mints its job.
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
