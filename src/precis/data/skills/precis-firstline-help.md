---
id: precis-firstline-help
family: memory
title: precis — first-line discipline for numeric-ref kinds
summary: the first line of a memory/todo/gripe is its entire scannable surface — lead with the conclusion, per-shape patterns inside
answers:
  - why does the first line of a memory or todo matter so much?
  - what's the actionable-axis pattern for something that points forward?
  - how do I write a good memory title and description?
  - which field does memory search match — title, hook, or first line?
applies-to: put (kind='memory'/'todo'/'gripe')
tags: drafting
kinds: memory, todo, gripe
status: active
---

# precis-firstline-help — write a first line that gets the note *used*

**Memory shape card**
- One claim per node; no second topic.
- The title IS the claim: at most 12 words, the sūtra, with its scope.
- The body is the gloss: why, when it bites, what to do, one live anchor, a UTC date.
- Edges carry roles: `part-of` the hub, `cites`/`derived-from` for evidence, `qualifies` for scope.
- Name entities by handle; their current state belongs on their refs, never restated here.
- Expiry follows shape (gr560763): threads/handoffs/resumes/snapshots 14 days; incidents/campaigns 90 days.
- Rulings use `review_by`; reference/gotchas/runbooks have no age expiry; DREAM never expires by age.
- Expiry means review and compact before retirement, preserving still-true claims.
- No landed history or children lists; use the graph's links.
- No plan label (slice/round/R-number/leg/wave) without its handle on the same line.
- No bare commit sha older than 30 days; cite a live `path/file.py::Symbol` instead.

On any numeric-ref kind (`memory`, `todo`, `gripe`) the
**header is the entire scannable surface** — it's what shows in
`/recent` listings, search hits, the grid, and review tiers. Everything
else is body you only see after you've already decided to open it.

For `memory`, that header is the **`title=`** field, backed by two more
retrieval keys — `meta.hook` and the body's first line — defined under
"Retrieval keys" below. For the other kinds the first line of the body
*is* the header, by being first.

## Retrieval keys — title vs hook vs first body line

Three fields decide whether a `memory` is found again — defined once
here, every other doc points here:

- **`title=`** — the header shown in listings, search hits and the
  grid; matched (with the hook) by `search(kind='memory', q=…,
  scope='title')`. Carry the terms you will search for — entity,
  number, scope — in at most 12 words.
- **`meta.hook`** — the one-line index text `view='index'` and recall
  print after the title (repo-dev convention: status + resume point);
  also matched by `scope='title'`.
- **first body line** — the fallback for both: `title=` is derived
  from it when omitted (capped at 80 chars), and the index line falls
  back to it when there is no hook.

The 80-character cap is the automatic fallback's truncation rule; the
12-word limit is authoring guidance. Set `title=` to a complete claim
instead of relying on truncation to produce one.

## The principle

Lead with the **conclusion, not the topic** — what you'd say if asked,
not what the note is "about". Include the one distinguishing detail
(scope, number, ref id). Skip filler:

- ❌ `Notes on the chunker refactor`
- ❌ `Memory about why we picked bge-m3`
- ❌ `Re: dream cadence`
- ✅ `Recursive splitting prevents single-chunk transcripts.`
- ✅ `bge-m3 beat e5 on our retrieval set (nDCG 0.71 vs 0.64).`

Headers are plain text, without `#` or bolding. For a memory, separate
the claim from its gloss:

```python
put(
    kind="memory",
    text="For long-context transcripts, use bge-m3: e5 missed more relevant "
    "passages in the 2026-11-03 UTC evaluation. Source: <evaluation handle>.",
    title="bge-m3 beat e5 on our retrieval set (nDCG 0.71 vs 0.64)",
    tags=["topic:embeddings", "confidence-strong"],
)
```

## Per-shape patterns

Pick the shape that fits, then write the header to match — the memory
shapes go in `title=`, todo/gripe in the first body line. These are
header *patterns*, not tags; tag vocabulary lives in `precis-tags` (on
repo-dev the node type is the `section:<type>` tag, `precis-memory-help`).

```
shape           header pattern
decision        Decided: <what> (+ short reason if it fits)
finding         <conclusion> (<scope or source>)
open question   Open: <the literal question>
distilled       <the distillation, one sentence>
ref-anchor      <source>: <what's there>
thought         <the thought, first person>
todo            imperative + the very next physical action (GTD: next step, not the outcome)
gripe           symptom first, not cause
```

## The actionable axis (for anything that points forward)

For anything you'll re-read and act on later — a forward-looking thought,
interest, decision, todo or gripe — carry these in the body/gloss:

- **trigger** — when this matters (*"next time I touch watcher routing…"*)
- **action** — the concrete next step
- **why + anchor** — the reason, plus a `kind:id` ref so it's verifiable

No dangling "this / that / it" — name the thing.

## See also

- [[precis-memory-help]] — memory capture mechanics
- [[precis-todo-tree-help]] — todo shapes + GTD next-action
- [[precis-gripe-help]] — the bug tracker
