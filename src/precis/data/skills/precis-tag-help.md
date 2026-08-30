---
id: precis-tag-help
title: precis — the tag verb (add and remove tags)
summary: the tag verb mechanics — atomic add/remove, axis replacement, state transitions
answers:
  - how do I add or remove tags on a ref that already exists?
  - how do I move a todo from open to done without leaving a stale STATUS tag?
  - what's the difference between an UPPERCASE axis tag and a lowercase open tag?
  - how do I toggle a bare flag tag like pinned?
applies-to: tag (every kind that supports it)
status: active
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
tag(kind="paper", id="wang2020state", add=["topic:noxrr"])
tag(kind="memory", id=42, remove=["pinned"])
```

`add=` and `remove=` are both lists. Either can be omitted; both
in one call is a single atomic update. `remove=` of a tag the ref
doesn't have is a no-op.

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

```python
get(kind="skill", id="precis-tags")  # axis vocabulary + per-kind matrix
get(kind="skill", id="precis-paper-tag-axes")  # paper-specific axes
get(kind="skill", id="precis-put-help")  # tags= at creation
get(
    kind="skill", id="precis-relations"
)  # link verb (typed cross-refs, distinct from tags)
get(kind="skill", id="precis-session-context-help")  # PRECIS_DEFAULT_TAGS hint surface
```
