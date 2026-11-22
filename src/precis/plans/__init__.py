"""precis.plans — plans as todo subtrees (dark factory step 3).

Spec: `me692430` § "Plans: the job lifecycle and the
plan graph". A plan is a todo subtree whose nodes are typed by ``meta``
on existing fields, never by a new kind:

- task: a leaf with a brief; ``meta.op`` names the job_type (default
  ``run_slice`` for LLM work);
- phase: a todo with children and ``meta.auto_close``. Dispatch waits for
  work below closed intermediate todos too, so closing a phase cannot
  remove those descendants from the candidate scan;
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
  are judged on declared write sets or labelled edit requests so evidence
  does not divert builder work; graph-only checks retain the full report.
  A todo's body and newest brief keep separate deny-path scopes: body
  labels must not hide the brief's requested edit.

The dispatcher tick lives with the other job types
(``precis.workers.job_types``), not here: this package holds the plan
model and its pure evaluators, the worker side holds the pass.
The tick stores the current dispatch hold on the todo as
``meta.dispatch_hold = {reason, since}``, so operators can inspect it
independently of the tick summary. Identical reasons keep their first
observed UTC time. File overlap can hold a queued job at claim time,
so the tick checks queued builds as well as unminted candidates. A source
gripe's open blocker holds a new build before mint and an existing job
at claim. The chain and node stall checks also read the live edge because
a sequenced build can be jobless and inactive for the entire stall window.

A task class without an enabled lane is a configuration gap, not capacity
pressure: dispatch blocks the node on one coordinator ask before minting a
slice. Submit checks again for configuration changes after minting. Full
lanes, health parks and host backoffs retain the transient wait path; a
coordinator repairs the class offering and closes the ask to release it.

Validation submit proves model diversity using prior code-session records.
Coordinator escalation considers only hosts and retry lanes the job can
reach. An eligible remote Codex host resolves its own account default
before launch because model defaults can vary by host.
"""
