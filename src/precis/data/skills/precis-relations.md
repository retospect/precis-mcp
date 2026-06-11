---
id: precis-relations
title: precis — relation vocabulary for link(rel=)
applies-to: link (rel=), put (rel= on create)
status: active
---

# precis-relations — relation vocabulary for `link(rel=)`

Closed list of relation slugs `link(...)` accepts as `rel=`. Default
is `related-to`. Unknown relations raise `BadInput` with the full
options list. Link verb mechanics and target grammar live in
`precis-link-help`.

## Which relation should I use?
## Pick the right rel= for what I want to record
## I want to link A to B — which relation fits?

| `rel=` | Inverse | Use for |
|---|---|---|
| `related-to` (default) | self | Symmetric "see also"; no stronger claim fits. |
| `cites` | `cited-by` | A references B (paper → paper, memory → paper, etc.). |
| `supports` | `supported-by` | B is evidence for A. |
| `contradicts` | `contradicted-by` | A disagrees with B. |
| `derived-from` | `derived-into` | A was produced from B (summary, distillation, chase result). |
| `generalises` | `specialises` | A is the broader abstraction of B. |
| `blocks` | `blocked-by` | A workflow item must finish before B can. |
| `see-also` | (none) | One-way "for context" pointer with no reverse semantic. |
| `retracts` | `retracted-by` | A retracts B (notice → paper). |
| `corrects` | `corrected-by` | A corrects B (corrigendum → paper). |
| `raises-concern-about` | `concern-raised-by` | A raises an expression of concern about B. |
| `fixes` | `fixed-by` | A workflow job resolves a gripe / todo (job → gripe is the canonical pair; pairs with `job_type='fix_gripe'`). |
| `supersedes` | `superseded-by` | A subsumes B; B is soft-deleted but graph-reachable via the inverse. Used by `memory.supersede` consolidation (survivor → originals). |

All relations except `related-to` and `see-also` auto-mirror: writing
`cites` from A→B makes A→B queryable as `cited-by` from B's side
without a second `link()`.

## Cite a paper from a memory or another paper
## Record that A cites B
## How do I capture a citation edge?

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites')

link(kind='memory', id=42,
     target='paper:wang2020state~38', rel='cites')   # cite a specific block
```

Use `cites` for any reference edge — bibliographic citation, in-body
mention, or quoted passage. Block-level targets pin the citation to
one paragraph.

## Record evidential support or disagreement
## A backs / counters B — which rel?
## I have a memory that agrees (or argues against) a paper

```python
link(kind='memory', id=89,
     target='paper:wang2020state', rel='supports')

link(kind='memory', id=89,
     target='paper:chen2021critique', rel='contradicts')
```

`supports` / `contradicts` carry an evidential claim — stronger than
`cites`. Use when the source ref takes a position on the target's
findings. Both work between any two ref kinds.

## Record provenance (A came from B)
## How do I link a summary to its source?
## Mark one ref as derived from another

```python
link(kind='memory', id=12,
     target='paper:wang2020state', rel='derived-from')

link(kind='research', id=88,
     target='todo:14', rel='derived-from')
```

`derived-from` records that A's content was produced from B —
summaries, distillations, chase-pipeline outputs, manual notes
extracted from a passage.

## Express abstraction level
## A is broader / narrower than B

```python
link(kind='memory', id=51,
     target='memory:23', rel='generalises')          # 51 is the broader claim

link(kind='memory', id=23,
     target='memory:51', rel='specialises')          # equivalent edge from the other side
```

Use between concept-bearing refs (memory, fc, paper). The auto-mirror
means writing one direction makes the other queryable.

## Block one todo on another
## Record a workflow dependency
## A can't start until B is done

```python
link(kind='todo', id=141,
     target='todo:158', rel='blocked-by')

link(kind='todo', id=158,
     target='gripe:7', rel='blocks')
```

`blocks` / `blocked-by` is the workflow-filter pair. Targets are
usually `todo` or `gripe`. The `todo` list view filters on these.

## Mark a retraction, correction, or concern
## A retracts / corrects / raises concern about B

```python
link(kind='memory', id=7,
     target='paper:badpaper2020', rel='retracts')

link(kind='paper', id='corrigendum-slug',
     target='paper:original-slug', rel='corrects')

link(kind='memory', id=8,
     target='paper:disputed2021', rel='raises-concern-about')
```

These attach a provenance notice to the affected ref. The renderer
surfaces the inverse (`retracted-by`, `corrected-by`,
`concern-raised-by`) when displaying the target.

## One-way "for context" pointer
## I want to nudge a reader toward B without claiming a stronger edge

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='see-also')
```

`see-also` is the only asymmetric relation with **no** inverse. Use
for "while reading A, you might want B" hints that don't fit
`related-to`, `cites`, or any evidential edge.

## Default — symmetric "see also"
## I just want a generic link, no specific claim

```python
link(kind='memory', id=47,
     target='paper:wang2020state')                  # rel='related-to' by default
```

`related-to` is symmetric — querying from either side surfaces the
edge without a separate inverse row. Omit `rel=` to get it.

## See also

```python
get(kind='skill', id='precis-link-help')        # link verb mechanics, target=, mode=
get(kind='skill', id='precis-tags')             # tag vocabulary (axes vs relations)
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-todo-help')        # blocks/blocked-by workflow filter
get(kind='skill', id='precis-citation-help')    # verifier workflow for cites
get(kind='skill', id='precis-provenance-help')  # retraction/correction notices
```
