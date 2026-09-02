---
id: precis-relations
title: precis — relation vocabulary for link(rel=)
summary: closed relation vocabulary — cites, supports, disputes, contradicts, derived-from, blocks, retracts, corrects
answers:
  - which relation do I use to link two refs — cites, supports, or something else?
  - how do I record that a memory agrees with or disagrees with a paper?
  - how do I mark one ref as derived from another?
  - how do I block one todo on another?
  - how do I record a retraction or correction relation?
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
| `disputes` | (none) | A appears to conflict with B — free to file, never blocks. The open-question edge for claim-graph disagreement (memory-vs-memory too). |
| `contradicts` | `contradicted-by` | Claim-graph disagreement, **adjudication-derived only** — file `disputes`, not this. Still valid, unaffected vocabulary for `memory`↔`memory` (a different subsystem, memory reconsolidation). |
| `derived-from` | `derived-into` | A was produced from B (summary, distillation, chase result). |
| `refines` | (none) | A is a higher-fidelity treatment of the same object as B — no inverse (advisory only). Two independent uses: a sharper/reworded taproot claim hub → the coarser claim hub it refines; a verify-tier (coadsorbed) reaction `pathway` → its neb-tier (parked) sibling on the same catalyst candidate. |
| `conjunct-of` | (none) | Taproot only: A (an atomic claim hub) is one conjunct of B (the compound hub bundling it) — no inverse, advisory-only, written by `taproot.hub.apply_extraction`'s decomposition, not hand-authored. |
| `generalises` | `specialises` | A is the broader abstraction of B. |
| `blocks` | `blocked-by` | A workflow item must finish before B can. |
| `see-also` | (none) | One-way "for context" pointer with no reverse semantic. |
| `retracts` | `retracted-by` | A retracts B (notice → paper). |
| `corrects` | `corrected-by` | A corrects B (corrigendum → paper). |
| `raises-concern-about` | `concern-raised-by` | A raises an expression of concern about B. |
| `fixes` | `fixed-by` | A workflow job resolves a gripe / todo (job → gripe is the canonical pair; pairs with `job_type='fix_gripe'`). |
| `supersedes` | `superseded-by` | A subsumes B; B is soft-deleted but graph-reachable via the inverse. Used by `memory.supersede` consolidation (survivor → originals). |
| `parent` | (children) | A is placed under B in the todo tree (todo only). `mode='remove'` detaches A to a root. |
| `entails` | `entailed-by` | A (an inference node) logically yields B (its conclusion lemma) — asserted, not proven. Premises attach to the inference with `derived-from`; see "Record a reasoning step" below. |
| `qualifies` | `qualified-by` | A (a caveat node) limits/bounds B (the claim it caveats). Surfaced — never auto-discharged — by `get(view='argument')`. |
| `cited-in` | (none) | Paper A is woven into and cited by document B (a topic dossier `draft`) — a `citation` exists. |
| `corroborates` | (none) | Paper A supports an existing point already woven into document B, grouped with it. |
| `superseded-in` | (none) | Paper A is subsumed by a later/review paper already integrated into document B; recorded, not separately woven. |
| `off-topic-for` | (none) | Paper A was considered for document B and rejected as out of scope. |

All relations except `related-to` and `see-also` auto-mirror: writing
`cites` from A→B makes A→B queryable as `cited-by` from B's side
without a second `link()`. `cited-in` / `corroborates` / `superseded-in` /
`off-topic-for` are the exception among the *non*-`related-to`/`see-also`
set — asymmetric with NO inverse (like `see-also`): a paper's disposition
toward a dossier reads from the dossier side via `get(kind='draft',
id=<dossier-slug>, view='integration')`, not a mirrored `link()` read.

## Cite a paper from a memory or another paper
## Record that A cites B
## How do I capture a citation edge?

```python
link(kind="memory", id=42, target="pa<id>", rel="cites")  # ref handle

link(
    kind="memory", id=42, target="pc38", rel="cites"
)  # chunk handle — cite a specific block
```

Use `cites` for any reference edge — bibliographic citation, in-body
mention, or quoted passage. Block-level targets pin the citation to
one paragraph. Lead targets with the ref/chunk **handle** (`pa<id>`,
`pc38`).

## Record evidential support or disagreement
## A backs / counters B — which rel?
## I have a memory that agrees (or argues against) a paper

```python
link(kind="memory", id=89, target="pa5", rel="supports")

link(kind="memory", id=89, target="pa6", rel="disputes")
```

`supports` / `disputes` carry an evidential claim — stronger than
`cites`. Use `disputes`, never `contradicts`, when the source ref
takes a position *against* the target's findings: `contradicts` is
adjudication-derived (claim-graph disagreement, unbuilt Part 2) and
not fileable this way — `disputes` is the free, non-blocking
equivalent and works between any two ref kinds. (Exception:
`memory`↔`memory` `contradicts` is a separate, unaffected subsystem —
`precis-memory-help`.)

## Record provenance (A came from B)
## How do I link a summary to its source?
## Mark one ref as derived from another

```python
link(kind="memory", id=12, target="pa5", rel="derived-from")

link(kind="perplexity-research", id=88, target="todo:14", rel="derived-from")
```

`derived-from` records that A's content was produced from B —
summaries, distillations, chase-pipeline outputs, manual notes
extracted from a passage.

## Express abstraction level
## A is broader / narrower than B

```python
link(
    kind="memory", id=51, target="memory:23", rel="generalises"
)  # 51 is the broader claim

link(
    kind="memory", id=23, target="memory:51", rel="specialises"
)  # equivalent edge from the other side
```

Use between concept-bearing refs (memory, anki, paper). The auto-mirror
means writing one direction makes the other queryable.

## Block one todo on another
## Record a workflow dependency
## A can't start until B is done

```python
link(kind="todo", id=141, target="todo:158", rel="blocked-by")

link(kind="todo", id=158, target="gripe:7", rel="blocks")
```

`blocks` / `blocked-by` is the workflow-filter pair. Targets are
usually `todo` or `gripe`. The `todo` list view filters on these.

## Mark a retraction, correction, or concern
## A retracts / corrects / raises concern about B

```python
link(kind="memory", id=7, target="pa7", rel="retracts")

link(kind="paper", id="corrigendum-slug", target="pa8", rel="corrects")

link(kind="memory", id=8, target="pa9", rel="raises-concern-about")
```

These attach a provenance notice to the affected ref. The renderer
surfaces the inverse (`retracted-by`, `corrected-by`,
`concern-raised-by`) when displaying the target.

## Record a reasoning step (argument graph)
## Chain lemmas into an inference; state a conclusion that follows

```python
# premises attach to the inference with derived-from (reused, not a new
# relation — "the inference was produced from its premises")
link(
    kind="memory",
    id=501,  # the kind:inference node
    target="me<lemma-A-id>",
    rel="derived-from",
)
link(kind="memory", id=501, target="me<lemma-B-id>", rel="derived-from")

# the inference entails its conclusion — a fresh, reusable kind:lemma
link(kind="memory", id=501, target="me502", rel="entails")

# a caveat qualifies the claim it limits
link(kind="memory", id="<caveat-id>", target="me<lemma-or-finding-id>", rel="qualifies")
```

Both pairs auto-mirror like every other directed relation here —
`entailed-by` and `qualified-by` are queryable from the target's side
without a second `link()` call. Full workflow (stating lemmas, the
`meta.rule`/`meta.warrant` operator vocabulary, reading the proof tree,
caveat propagation): `precis-argument-help`.

## One-way "for context" pointer
## I want to nudge a reader toward B without claiming a stronger edge

```python
link(kind="memory", id=42, target="pa5", rel="see-also")
```

`see-also` is asymmetric with **no** inverse. Use for "while reading A,
you might want B" hints that don't fit `related-to`, `cites`, or any
evidential edge. (The four `cited-in`/`corroborates`/`superseded-in`/
`off-topic-for` integration-disposition relations, below, are the same
shape — no inverse — but a different semantic.)

## Record a paper's disposition toward a topic dossier
## Mark a paper as woven-in / corroborating / superseded / rejected

```python
link(
    kind="paper",
    id="wang2020state",
    target="draft:mof-review~current-state",
    rel="cited-in",
)  # woven into that section, cited
link(kind="paper", id="miller23", target="draft:mof-review", rel="off-topic-for")
```

Direction is always **paper → dossier** (the `draft`); the optional
target selector (`~<section>`) anchors the disposition to one section.
No inverse — read a dossier's ledger from the dossier side:
`get(kind='draft', id='mof-review', view='integration')` (INTEGRATED,
grouped by section, vs PENDING — `topic:`-tagged papers with no
disposition edge yet). See `docs/backlog/paper-writing-pipeline.md`
§"The integration ledger".

## Default — symmetric "see also"
## I just want a generic link, no specific claim

```python
link(kind="memory", id=47, target="pa5")  # rel='related-to' by default
```

`related-to` is symmetric — querying from either side surfaces the
edge without a separate inverse row. Omit `rel=` to get it.

## See also

```python
get(kind="skill", id="precis-link-help")  # link verb mechanics, target=, mode=
get(kind="skill", id="precis-tags")  # tag vocabulary (axes vs relations)
get(kind="skill", id="precis-overview")  # verbs and kinds
get(kind="skill", id="precis-todo-help")  # blocks/blocked-by workflow filter
get(kind="skill", id="precis-citation-help")  # verifier workflow for cites
get(kind="skill", id="precis-provenance-help")  # retraction/correction notices
get(
    kind="skill", id="precis-argument-help"
)  # entails/qualifies workflow, meta.rule/warrant
```
