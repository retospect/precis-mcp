---
id: precis-tag-help
family: core
title: precis — the tag verb (add and remove tags)
summary: the tag verb mechanics — atomic add/remove, axis replacement, state transitions
answers:
  - how do I add or remove tags on a ref that already exists?
  - how do I add the same tag to many refs?
  - how do I move a todo from open to done without leaving a stale STATUS tag?
  - what's the difference between an UPPERCASE axis tag and a lowercase open tag?
  - how do I toggle a bare flag tag like pinned?
applies-to: tag (every kind that supports it)
status: active
tags: verbs, workflow
scope: write
---

# precis-tag-help — add and remove tags on a ref

`tag` mutates a ref's tag set. `add=` and `remove=` are both
accepted in one call so a state transition lands atomically. For
the axis vocabulary (which prefixes mean what, which kinds accept
which axis), see `precis-tags`. This file is the verb mechanics.

## Change a tag on something I already have
## Add or remove tags after the ref exists
## I need to update tags on a ref

```python
tag(kind="todo", id=158, add=["STATUS:done"], remove=["STATUS:open"])
tag(kind="alert", id=42, add=["alert-state:resolved"])  # lowercase but exclusive — drops alert-state:open
tag(kind="paper", id="wang2020state", add=["topic:noxrr"])
tag(kind="memory", id=42, remove=["pinned"])
```

`add=` and `remove=` are both lists. Either can be omitted; both
in one call is a single atomic update.

Pass `reason=` to say why — one `agent/retagged` row per affected
ref lands on `ref_events` (`view='log'`), the audit line a mass tag
fold otherwise lacks:

```python
tag(kind="memory", id=[172, 173], remove=["stale"], reason="folded into me77")
```

`meta=` is a todo-only door on `tag` — an allowlisted key promotion
([[precis-todo-tree-help]]) — refused on every other kind. Other refs'
meta moves through `put(meta=…)` / `edit(args={'meta': …})`.

## Tag many refs in one call
## Add the same tag to many refs at once
## How do I add the same tag to many refs?

```python
tag(kind="todo", id=[158, 159], add=["STATUS:done"], remove=["STATUS:open"])
tag(kind="memory", id=[172, 173], remove=["stale"], reason="folded into me77")
tag(kind="paper", id=["pa1190", "dynamat26", 160060], add=["aixmat-2026"], reason="conference reading list")
```

`id=[...]` applies the same `add=` / `remove=` to every ref in the
list in one transaction — one call replaces N single-id calls.
Batch kinds include paper, todo, memory, gripe, job, alert and finding.
Quest, orcid, conv, md, tex, python and plaintext take one id per call.
An empty list refuses. A missing id or failed edit aborts the whole batch.

Paper accepts slugs, `pa` handles and numeric ref ids in the same list.
Omit `kind=` for a list of `pa` handles. Duplicate addresses for the same
paper collapse to one edit. Paper has no batch-size cap. The receipt starts
with `tagged N paper refs in one transaction` and follows with one
acknowledgment per distinct paper. Numeric-ref batch kinds accept up to
50 ids per call.

`reason=` writes one `agent/retagged` row per affected ref, including one
per distinct paper, on `ref_events` (`view='log'`).

## Bump a workflow STATUS atomically
## Move a todo from open to done in one call
## How do I transition state without a stale tag lingering?

```python
tag(
    kind="todo",
    id=42,
    add=["STATUS:done", "PRIO:low"],
    remove=["STATUS:open", "PRIO:high"],
)
```

`PRIO:` and `prio=` are one axis on every kind (todo, gripe, quest,
job): an integer 1..10, **lower = hotter**, any value allowed. The names
`urgent`/`high`/`normal`/`low` are aliases for 1/3/5/8; `PRIO:4` and
`prio=4` are as valid as `PRIO:high`. The value is stored in the `prio`
column, not as a tag row.

Pair `add=` + `remove=` in one call to flip state cleanly. With
closed UPPERCASE prefixes the explicit `remove=` is belt-and-braces
— see the next section.

## Which shape does my tag call need?

```python
tag(kind="todo", id=158, add=["STATUS:done"])  # UPPERCASE — implicitly drops any prior STATUS:* on this ref
tag(kind="paper", id="wang2020state", add=["topic:noxrr", "project:foo"])  # lowercase — accumulates freely
tag(kind="memory", id=42, add=["pinned"])  # bare flag — on/off toggle
```

Axis vocabulary (which prefixes exist, per-kind matrix, open-tag
naming) is `precis-tags`; this is the verb-call shape only.

## Tag a ref at creation time

```python
put(
    kind="todo",
    text="Review section 3 of abazari2024design.",
    tags=["PRIO:high", "topic:photocatalysis"],
)
```

`put(..., tags=[...])` applies tags as part of the create. Use the
`tag` verb for any later change.

## What if an axis isn't allowed on this kind?

```text
[error:BadInput] axis 'STATUS' not allowed on kind 'paper'
```

Closed prefixes are gated per kind (e.g. `STATUS:` on workflow
kinds, `SRC:`/`CACHE:` on provenance kinds). The matrix lives in
`precis-tags`; the error names the offending axis.

## See also

- [[precis-tags]] — axis vocabulary + per-kind matrix
- [[precis-paper-tag-axes]] — paper-specific axes
- [[precis-put-help]] — tags= at creation
- [[precis-relations]] — link verb (typed cross-refs, distinct from tags)
- [[precis-session-context-help]] — PRECIS_DEFAULT_TAGS hint surface
