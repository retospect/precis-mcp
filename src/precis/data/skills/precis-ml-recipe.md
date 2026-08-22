---
id: precis-ml-recipe
title: precis — extract ranked training recipes from the corpus
summary: recipe-extraction runbook — one entry per claimed result (dataset/method/hyperparameters/compute/code path), verified/unverified/blocked/inferred labels, ranked brief
answers:
  - how do I extract a reproducible recipe from a claimed ML result?
  - how do I check whether the dataset and code for a result are actually available?
  - how do I record my confidence per field instead of one blanket confidence?
flavor: runbook
status: active
applies-to: search/get (kind='paper','citation','web'); put (kind='draft','plan')
---

# precis-ml-recipe — extract ranked, implementable recipes from the corpus

Adapted from companion-inc/feynman (MIT). Turns a task or paper into
a ranked list of training recipes backed by what the corpus actually
holds — not a literature summary.

## Start from evidence of a result, not from a script

```python
search(kind="paper", q="<task> benchmark result")
search(kind="citation", q="<task>")
```

A recipe entry needs a stated result to anchor it — start from the
number, then trace back to its setup.

## Build one entry per candidate approach

Per paper/result: dataset, method, hyperparameters, compute
assumptions, metric, and the code path that produced it (repo
file/function if the paper names one).

```python
get(kind="paper", id="<slug>~lo..hi")  # the methods/results chunks
```

## Check dataset and code availability, don't assume it

```python
get(kind="web", id="<dataset-or-repo-url>")
```

No URL to check, or the fetch fails? Label the entry `unverified`,
not usable-as-is.

## Rank and write the brief

A ranked brief needs: one recommendation (the recipe to try first,
and why), a table of every candidate, known gaps, and sources.

```python
put(kind="draft", id="<task>-recipe", project=<todo-id>, title="<Task> training recipes")
put(
    kind="draft",
    id="<task>-recipe",
    chunk_kind="table",
    table={
        "header": [
            "approach",
            "result",
            "dataset",
            "method",
            "hyperparameters",
            "compute",
            "code",
            "status",
        ],
        "rows": [["<...>"] * 8],
    },
    at={"last": True},
)
```

Prefer `kind='plan'` over `draft` when the brief is scratch work for
your own next step rather than a deliverable to hand off.

## Say exactly how sure you are, per field

`verified` (checked directly) / `unverified` (not checked, don't
imply usable) / `blocked` (checked and unavailable) / `inferred`
(reasoned, not read). Apply per field, not per entry — a recipe can
be `verified` on dataset and `inferred` on hyperparameters in the
same row.

## Cite the exact source behind every field

Cite the chunk backing each recipe field: `[pc<id>]` for a paper
passage. A field you're still chasing gets a `finding`, not a guess.

## See also

```python
get(kind="skill", id="precis-replication")  # design a check before you run one
get(
    kind="skill", id="precis-paper-code-audit"
)  # verify a recipe against its actual code
get(kind="skill", id="precis-finding-help")  # chase a cited-but-unheld source
get(kind="skill", id="precis-draft-help")  # the document kind used for the final brief
get(kind="skill", id="precis-plan-help")  # scratch-work alternative to a draft
```
