---
id: precis-replication
title: precis — plan a replication check for a paper's claim
summary: replication-planning runbook — extract the exact setup behind a claimed result, choose plan-only vs dispatched execution, never call it replicated until the checks pass
flavor: runbook
status: active
applies-to: search/get (kind='paper','web'); put (kind='plan','todo','finding','memory')
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

Precis has no execution sandbox — decide before writing anything down:

- **Plan only** (default) — persist the plan; a human runs it.
- **Dispatch** — mint a todo an operator or worker can pick up
  (`meta.executor` for automated dispatch — `precis-dispatch-help`).

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

## Never call it replicated until the checks actually passed

Report success only once the *planned* checks ran and passed — not
once the plan is written, and not because the code looks right.

## Log progress on the plan, not a changelog

```python
put(kind="memory", text="replication attempt 1: dataset unavailable, blocked on X")
edit(kind="plan", id="pe<node-id>", status="done")
```

The plan node is the record of what happened — no separate log
artifact.

## File a finding for every mismatch

```python
put(
    kind="finding",
    title="<mismatch summary>",
    body="paper claims <X>; the check found <Y>",
    cited_in="pc<id>",
)
```

A finding here is evidence a check disagreed with the paper — always
link it back to the exact source chunk (`cited_in=`), never to your
own plan or notes.

## See also

```python
get(kind="skill", id="precis-plan-help")  # the reasoning-outline kind
get(kind="skill", id="precis-finding-help")  # register a mismatch as a citation target
get(kind="skill", id="precis-ml-recipe")  # extracting a recipe in more depth
get(kind="skill", id="precis-web-help")  # fetch the paper's code repo
get(kind="skill", id="precis-tasks-help")  # dispatch execution as a todo
get(kind="skill", id="precis-dispatch-help")  # meta.executor for automated dispatch
```
