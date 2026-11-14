---
id: precis-link-reason-help
family: core
title: precis — writing the reason on a link (the edge's why)
summary: what goes in link(..., reason=) — the incremental information an edge adds beyond titles and relation, when '.' is right, what the write door refuses, and how to say a candidate was NOT useful
answers:
  - what do I write in reason= when linking a memory to a paper?
  - when is reason='.' right?
  - how do I say why a paper was NOT useful here?
  - why did the link door refuse my reason?
  - what do I do with an old link that has no reason?
  - the row turned "· suspect" — how do I fix the reason?
applies-to: link (memory/finding/paper/taxon/todo/gripe sources), rel='considered'
tags: verbs, addressing
status: active
scope: write
---

# precis-link-reason-help — the edge's why

`link(..., reason='…')` stores `links.meta.reason`. Every link surface
(Links table, `view='links'`, the eye's ring rows) appends
` — <reason>` to the row.

## What do I write in reason=?
## What is a reason for?

A reason is the **incremental information** the edge adds beyond the
displayed titles/summaries and the relation label: the bridge. Which
result supports which premise, under what condition, or why a candidate
fails this need. Short phrases suffice.

```python
link(kind="memory", id=42, target="pa5", rel="cites",
     reason="detailed derivation of the O* binding energy")
```

- Never restate the neighbour's title or summary, or the relation in
  prose ("cites this paper"). Never "relevant to this topic".
- Preserve direction: the reason explains A→B. Do not invent reciprocal
  usefulness for B→A.
- Useful counter-evidence is a `disputes` edge with a reason, never a
  negative verdict.

## When is reason='.' right?
## What if I have nothing to add?

`'.'` (or `'reviewed-obvious'`) renders as a bare period: reviewed, and no
explanation is needed because the titles and the relation already say it
all. It does NOT mean the claim is true.

Not knowing the connection is *missing/unresolved*, never obvious. Leave
`reason` absent and say so in the row's note or a todo; do not write text
just to clear a queue. Backfilling an old link records a present-day
reviewed rationale, not a story about the original author's intent.

## Why did the link door refuse my reason?

The door refuses a reason that restates an endpoint's title or displayed
summary verbatim (whitespace-collapsed equality), naming the duplicated
field. Two recoveries: write the contribution, or `'.'`. There is no
word-overlap gate: shared vocabulary alone passes.

## How do I say why a paper was NOT useful here?

`rel='considered'` from the scope (memory/quest/research node) to the
judged source. The why rides inside the verdict block, not `reason=`:

```python
link(kind="memory", id=73, target="pa5", rel="considered",
     meta={"verdict": "not-useful", "depth": "abstract",
           "reason": "bulk only; the question asks about surfaces"})
```

The reason is the "not a match because": name the scope mismatch (host,
facet, reaction family, bulk vs surface …) and say what depth you read
to. Verdict rules, ledger and `novel_for`:
`precis-memory-help` §Record a considered verdict on a source.

## Worked examples

| Situation | Reason | Note |
|---|---|---|
| Reason repeats the paper title | `"Operando XAS of Fe oxides"` | refused; write `"Fe(III)→Fe(II) shift under bias"` or `'.'` |
| Useful short phrase | `"detailed derivation"` | passes |
| Edge is obvious from titles + relation | `'.'` | reviewed-obvious |
| Not enough evidence to say | absent | note it in the row or a todo; unresolved is not obvious |
| Direction reversed | wrong: `"A gets its data from B"` on `A supports B` | the reason on A→B says what A gives B: `"A's rates constrain B's barrier"` |
| Candidate does not fit | `considered`, `"bulk only; question asks about surfaces"`, depth `abstract` | negative scope, not a `disputes` |
| Endpoint changed | row shows `· suspect` | re-read the target, then re-link with a fresh reason |

## See also

- [[precis-link-help]] — the link verb, `reason=` argument
- [[precis-relations]] — relation vocabulary, `considered`
- [[precis-memory-help]] — considered verdicts, the ledger
- [[precis-read-paper]] — where reasons get written in a reading pass
- [[precis-fisheye-help]] — how the why renders on ring rows
