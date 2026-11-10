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

Memory proposals pin supporting ref content as well as the edited body;
unpinned legacy proposals close before paid review. Stale memory evidence closes
the old proposal with a retryable rejection so unchanged target content cannot block
a fresh review. A proposal must match the fresh detector's action, checked
before paid review and again at apply, so supersede cannot degrade to retire.
Deterministic refusals close the key; unexpected deterministic apply failures
hold the proposal so they cannot buy another review each tick. Operational
database failures hold after five recorded attempts.
Memory approvals are saved separately before apply, survive rollback, and
are reused on bounded later-tick attempts after the same evidence checks.
If saving the approval itself fails, no apply occurs; a later tick may pay
again, at most once per proposal per tick within the shared review limit.
Hold-write failures count as errors without aborting the remaining tick.
The CLI shows hold reasons; rejecting an apply-error hold reopens its key.
Unresolved ops wait one day, then resolved ops get a fresh proposal. Applied find/span pairs must match
fresh detector ops, and link removals are refused, because model
payload overrides cannot expand the detector's edit scope. Undo writes a
terminal rejection on the restored content so the next tick cannot reapply it.
Supersede and retire snapshot links for undo, refusing changed endpoints.
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
   ``PRECIS_GARDEN_MEMORY_APPLY`` consumer gate; no-model successor rewrites
   remain deferred to gr564554.
   ``thread_shape`` covers tagged thread children too: missing ``Left:``
   or terminal todo/gripe work proposes supersede by the nearest live
   ancestor with a current ``Left:``, else retire. A supersede proposal
   requires preserving needed figures in the ancestor's ``Earlier:`` line;
   retirement also requires compaction review. No edits occur at detection.
   Git ancestry on origin/prod is deliberately untested: the DB has no
   general ancestry oracle and thread_shape needs no network or git.
2. Proposer (:mod:`precis.graph.garden.proposer`) — one routed LLM call
   per candidate renders it into a structured verdict
   (``accept``/rationale). Memory-body candidates (the memory detectors)
   get the node's fisheye+1hop plus the ``precis-fix-memory-gripe-help``
   checklist pinned verbatim and answer the one-action JSON:
   rewrite / qualify / supersede / retire / no-change / ask (gr560762).
   Model placement is configuration: the call
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
   With ``PRECIS_GARDEN_MEMORY_APPLY`` enabled, memory rewrite/supersede/retire/split
   proposals from ``dangling_mention``, ``anchor_rot``, ``grab_bag``,
   ``griped`` and ``thread_shape`` share this review budget. Eligibility and
   apply-time re-checks use one detector registry so proposals that cannot
   apply do not spend a review. The six ``memory-review`` detectors stay
   pending without consumer review or state changes. ``qualify`` and ``ask``
   remain outside that consumer; an ``ask`` verdict files a ``memory-review``
   todo from the runner (tiered by smell class), never a proposal. A
   ``griped`` apply replays the gripe's route and closes it with a
   ``resolved:`` comment once the deterministic pass holds (gr561873).

**Memory reconciliation.** Memory candidates (including ``dup_pair``) and the
interactive ``precis-make-memory`` skill share the packaged memory-help policy
through :mod:`precis.utils.memory_reconcile`. Proposer, consumer and sampled
reviewers use the exact same section; a policy digest versions only memory
proposer rejections. This keeps identity, conflict and preservation judgments
consistent without moving interpretation into the detectors or broadening
their apply doors. Sampled memory reviews also read bounded body excerpts;
merge repointing does not synthesize a loser's unique claims into the winner.

**Scheduling.** The ``graph_garden`` job_type (``job_inproc`` executor)
runs one bounded pass per tick — ``params.pass`` picks the detector
subset (``merge|split|relink|prune|tag-sweep|siblings|paper-identity|memory|all``,
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
