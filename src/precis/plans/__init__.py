"""precis.plans — plans as todo subtrees (dark factory step 3).

Spec: ``docs/backlog/dark-factory.md`` § "Plans: the job lifecycle and the
plan graph". A plan is a todo subtree whose nodes are typed by ``meta``
on existing fields, never by a new kind:

- task: a leaf with a brief; ``meta.op`` names the job_type (default
  ``run_slice`` for LLM work);
- phase: a todo with children and ``meta.auto_close``;
- gate: ``meta.gate = {kind, args}`` — ``sha-in-prod``, ``tests-green``,
  ``answer``, ``not-before``; evaluated, never dispatched;
- loop: ``meta.loop = {max, until, body}``;
- generator: ``meta.generate = {role, from}``.

Edges: containment is ``parent_id``, sequence is a ``blocked-by`` link,
service is ``serves``.

Modules (each lands as its own slice under td540836):

- :mod:`precis.plans.nodes` — the meta shapes and ``node_kind(meta)``;
  the todo handler validates against them;
- :mod:`precis.plans.gates` — gate evaluators;
- :mod:`precis.plans.lanes` — lane records (adapter + credential pool +
  concurrency cap) the ``run_slice`` job type picks from;
- :mod:`precis.plans.templates` — subtree minters (gripe-fix, slice-tree,
  release-pass, pcb-pipeline) behind ``put(kind='todo', mode='plan')``.

The dispatcher tick lives with the other job types
(``precis.workers.job_types``), not here: this package holds the plan
model and its pure evaluators, the worker side holds the pass.
"""
