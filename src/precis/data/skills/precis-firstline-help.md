---
id: precis-firstline-help
title: precis — first-line discipline for numeric-ref kinds
summary: the first line of a memory/todo/gripe/flashcard is its entire scannable surface — lead with the conclusion, per-shape patterns inside
applies-to: put (kind='memory'/'todo'/'gripe'/'flashcard')
status: active
---

# precis-firstline-help — write a first line that gets the note *used*

On any numeric-ref kind (`memory`, `todo`, `gripe`, `flashcard`) the
**first line is the entire scannable surface** — it's what shows in
`/recent` listings, search hits, and review tiers. Everything else is
body you only see after you've already decided to open it.

## The principle

Lead with the **conclusion, not the topic** — what you'd say if asked,
not what the note is "about". Include the one detail that distinguishes
it from its neighbours (date, scope, ref id). Skip filler:

- ❌ `Notes on the chunker refactor`
- ❌ `Memory about why we picked bge-m3`
- ❌ `Re: dream cadence`
- ✅ `Switched the chunker to recursive-separator splitting — fixes the one-giant-chunk youtube bug.`
- ✅ `bge-m3 beat e5 on our retrieval set (nDCG 0.71 vs 0.64).`

No leading `#` heading, no bolding — the first line is the header *by
being first*. The body (everything after line 1) stays terse for the
same reason: no preamble, no "here's a memory about…".

## Per-shape patterns

Pick the shape that fits, then write the first line to match:

```
shape            first-line pattern
memory:decision  Decided: <what> (+ short reason if it fits)
memory:finding   <conclusion> (<scope or source>)
memory:open      Open: <the literal question>
memory:distilled <the distillation, one sentence>
memory:ref-anchor <source>: <what's there>
memory:thought   <the thought, first person>
todo             imperative + the very next physical action (GTD: next step, not the outcome)
gripe            symptom first, not cause
flashcard        the question, verbatim
```

## The actionable axis (for anything that points forward)

A scannable first line gets a note *seen*. For anything you'll re-read
and act on later (a forward-looking thought, an interest, a decision
that changes what you'll do), also carry three things or it's inert
when it resurfaces:

- **trigger** — when this matters (*"next time I touch watcher routing…"*)
- **action** — the concrete next step
- **why + anchor** — the reason, plus a `kind:id` ref so it's verifiable

No dangling "this / that / it" — name the thing.

## See also

```python
get(kind='skill', id='precis-memory-help')   # memory capture mechanics
get(kind='skill', id='precis-tasks-help')    # todo shapes + GTD next-action
get(kind='skill', id='precis-gripe-help')    # the bug tracker
```
