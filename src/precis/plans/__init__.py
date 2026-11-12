"""precis.plans — plans as todo subtrees (dark factory step 3).

Spec: ``docs/backlog/dark-factory.md`` § "Plans: the job lifecycle and the
plan graph". A plan is a todo subtree whose nodes are typed by ``meta``
on existing fields, never by a new kind:

- task: a leaf with a brief; ``meta.op`` names the job_type (default
  ``run_slice`` for LLM work);
- phase: a todo with children and ``meta.auto_close``;
- gate: ``meta.gate = {kind, args}`` — ``sha-in-prod``, ``tests-green``,
  ``answer``, ``not-before``, ``drc-clean``; evaluated, never dispatched;
- loop: ``meta.loop = {max, until, body}``;
- generator: ``meta.generate = {role, from}``.

Edges: containment is ``parent_id``, sequence is a ``blocked-by`` link,
service is ``serves``.

Modules (each lands as its own slice under td540836):

- :mod:`precis.plans.nodes` — the meta shapes and ``node_kind(meta)``;
  the todo handler validates against them;
- :mod:`precis.plans.gates` — gate evaluators;
- :mod:`precis.plans.lanes` — lane records (adapter + credential pool +
  concurrency cap) derived from ``llm_account`` rows, which the
  ``run_slice`` job type picks from;
- :mod:`precis.plans.boundary` — the security-boundary registry: the
  path sets and added-line rules a factory diff may trip, marking the
  land for the ``security-review`` lane (read-only until the A3 land
  wiring);
- :mod:`precis.plans.templates` — subtree minters (gripe-fix, slice-tree,
  release-pass, pcb-pipeline) behind ``put(kind='todo', mode='plan')``;
- :mod:`precis.plans.route` — the mint-time coordinator route
  (gr645163): the shared classifier naming work no builder lane may
  write (a deny-path brief or prod-graph-only work), run by
  ``gripe_dispatch`` on the gripe report and by ``plan_dispatch`` on
  each run_slice-bound todo's brief. Deny paths in structured reports
  are judged on explicit change-intent paragraphs so symptom evidence
  does not divert builder work; graph-only checks retain the full report.
  A todo's body and newest brief keep separate deny-path scopes: body
  labels must not hide the brief's requested edit.

The dispatcher tick lives with the other job types
(``precis.workers.job_types``), not here: this package holds the plan
model and its pure evaluators, the worker side holds the pass.

A task class without an enabled lane is a configuration gap, not capacity
pressure: dispatch blocks the node on one coordinator ask before minting a
slice. Submit checks again for configuration changes after minting. Full
lanes, health parks and host backoffs retain the transient wait path; a
coordinator repairs the class offering and closes the ask to release it.
"""
