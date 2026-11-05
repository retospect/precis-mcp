---
id: precis-replication
family: paper
title: precis — plan a replication check for a paper's claim
summary: replication-planning runbook — extract the exact setup behind a claimed result, choose plan-only vs dispatched execution, never call it replicated until the checks pass
answers:
  - how do I plan a replication of a paper's claimed result?
  - how do I decide between a plan-only replication and one that actually dispatches execution?
  - when can I call a result replicated?
flavor: runbook
status: active
applies-to: search/get (kind='paper','web','pathway','job'); put (kind='plan','todo','finding','memory','job','pathway')
tags: workflow, external-sources
kinds: paper, web, plan, todo, finding, memory, job, pathway
---

# precis-replication — plan a replication check for a paper's claim

Adapted from companion-inc/feynman (MIT). Plans (and optionally
dispatches) a check of whether a paper's claimed result actually
reproduces from its stated setup.

## Read the exact chunk backing the claim, not the abstract
## Gather what the paper actually says

```python
search(kind="paper", q="<claim or method>")
get(kind="paper", id="<slug>", view="toc")
get(kind="paper", id="<slug>~lo..hi")  # drill into the results section
```

## Find the code the paper points at

```python
get(kind="web", id="<repo-or-docs-url>")
search(kind="web", q="<repo name> implementation")
```

No code link in the paper? Note it as `unverified` rather than
guessing what a repository does.

## Extract one recipe entry per claimed result

One entry per result: dataset, method, hyperparameters, compute
assumptions, metric, and the chunk that states each. Mark anything
you couldn't confirm from the text `unverified` — never assume a
detail is usable because it seemed plausible.

## Choose plan-only or dispatch execution

Choose the route before dispatching:

- **Plan only** (default) — persist the recipe and its acceptance checks.
- **Sandbox execution** — `sandbox_run` supports `mode='script'` for a
  Python script with PyPI dependencies, `mode='build'` for a coding task,
  and `mode='run'` for a prior build's harvested artifact. It needs an
  enabled sandbox host and a wall-time budget; submission is not proof
  that execution started. See [[precis-job-observe-help]].
- **Pathway revalidation** — explicitly request the stored manifest's
  CPU replay; the job checks the lock and engine, runs the available CPU
  legs and judges recorded observables against the manifest's tolerances.
- **Operator execution** — mint a todo with the recipe when no supported
  execution route is available; see [[precis-minter-help]].

```python
put(
    kind="plan",
    id="<slug>-replication",
    title="Replicate <slug>",
    project=<todo-id>,
    text="dataset: <ds>; method: <method>; hyperparameters: <...>; metric: <metric>",
)
put(kind="todo", text="run replication check for <slug>", parent_id=<todo-id>)
```

## Request execution explicitly

```python
# A prepared script; runs offline after its dependencies and inputs are staged.
put(kind="job", parent_id=<todo-id>, job_type="sandbox_run", params={
    "mode": "script", "code_ref": "<script-ref>", "deps": ["<pinned-pypi-requirement>"],
    "inputs": ["<dataset-ref>"], "target_node": "<enabled-sandbox-host>",
    "resources": {"wall_seconds": 600},
})

# A pathway with a stored validation manifest; reads launch no compute.
get(kind="pathway", id="<pathway-handle>", view="manifest")
put(kind="pathway", id="<pathway-handle>", mode="revalidate",
    args={"manifest": "<manifest-id>"})
get(kind="pathway", id="<pathway-handle>", view="manifest")
```

Preserve `reproduced_partial(energies)` and `complete: false`: CPU replay
does not rerun relax/NEB energies. Missing TPD records, unstamped engine
commits and missing weights remain named limitations; a CPU replay does
not check MLIP weights. An incompatible lock refuses execution.

## Never call it replicated until the checks actually passed

Report each check's scope and evidence separately:

| Check | What a pass establishes |
|---|---|
| File integrity | Archived bytes match the recorded hashes |
| Execution | The named computation ran with the recorded outcome |
| Numerical agreement | Compared observables met the stated tolerance on the named platforms |
| Claim support | The cited evidence and existing review gates support the scoped claim |

A hash match or successful execution never establishes scientific validity.
Report replication only for planned checks that ran and passed; retain
failed, not-run and unavailable checks, tolerance differences and provenance
caveats. Reading an archived report launches no compute. Keep verification
records as append-only evidence beside the frozen run; do not rewrite it to
match a replay or use replay success to promote claim support.

## Log progress on the plan, not a changelog

```python
put(kind="memory", text="replication attempt 1: dataset unavailable, blocked on X")
edit(kind="plan", id="pe<node-id>", status="done")
```

Keep planning progress on the plan; retain the execution jobs and verification
reports as evidence linked from it.

## File a finding for every mismatch

```python
put(
    kind="finding",
    title="<mismatch summary>",
    body="paper claims <X>; the check found <Y>",
    args={"cited_in": "pc<id>"},
)
```

A finding here is evidence a check disagreed with the paper — always
link it back to the exact source chunk (`cited_in=`), never to your
own plan or notes.

## See also

- [[precis-plan-help]] — the reasoning-outline kind
- [[precis-finding-help]] — register a mismatch as a citation target
- [[precis-ml-recipe]] — extracting a recipe in more depth
- [[precis-web-help]] — fetch the paper's code repo
- [[precis-todo-tree-help]] — dispatch execution as a todo
- [[precis-minter-help]] — meta.executor for automated dispatch
- [[precis-job-observe-help]] — sandbox execution and compute routes
