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
through ``precis garden apply`` — never invoked by a pass. A pass over
a clean graph writes nothing: no reviews, no chunks, no log spam.

**Pipeline** (:mod:`precis.graph.garden.runner`):

1. Detectors (:mod:`precis.graph.garden.candidates`) — deterministic
   SQL/Python over the live graph. No LLM, no writes:
   ``dup_pair`` (embedding + title/alias similarity, dimension-gated
   for taxa), ``orphan`` (taxon reaching no start node), ``stale``
   (untouched, unlinked, unviewed node), ``dangling_edge`` (link to a
   retired ref) / ``cycle_edge`` (a 2-cycle in the taxon DAG),
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
   proposals with the same proposal key.

**Scheduling.** The ``graph_garden`` job_type (``job_inproc`` executor)
runs one bounded pass per tick — ``params.pass`` picks the detector
subset (``merge|split|relink|prune|tag-sweep|siblings|all``), ``limit``
bounds candidates scanned, ``max_proposals`` bounds writes. The
``graph_garden`` scheduler cadence mints at most one job per UTC day
(``meta.idem_key='garden:<date>'``), and only while the
``graph_garden`` service_config row is lit — dark by default
(:func:`precis.graph.garden.service.graph_garden_enabled`). After
``dry_rest_escalate`` consecutive empty runs the mint backs off to the
cooldown interval instead of minting daily forever.

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
