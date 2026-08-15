# Dossier prose emits paper handles bare, so they render as dead text

> Found 2026-08-15 verifying the dossier prose rewrite on quest 202469's
> dossier 202546 (that verification passed; this is the one residual).

## The rule

`precis/quest/tick.py`'s dossier-format prompt block (search `Reference
anything by copying its exact handle in square brackets`) tells the model:

> `[st<id>]` a candidate structure, `[pc<id>]`/`[pa<id>]` literature,
> `[fi<id>]` a finding — never invent one: **parentheses don't linkify** and a
> made-up handle resolves to nothing.

## What it actually produces

In the 2026-08-15 05:33 rewrite of chunk 2856653, `pc` handles obey the rule
and `pa` handles do not — bare, or worse, parenthesised:

- correct: `… solvable in a protein active site [pc2858232]`, `[pc2855136]`,
  `[pc2593610]`
- bare: `A targeted search linked pa206506 for this MR-1/CD system`
- parenthesised, the case the prompt explicitly warns about: `Two further
  papers linked this tick (pa208059, pa208060)`, `(pa205190, pa205191,
  pa202726)`, `(pa208054, pa208055, pa208058)`

Roughly 25 `pa` references in that one chunk, none bracketed. Every one renders
as dead text in the draft — the same class of complaint as the raw-markdown bug
this rewrite fixed, just narrower.

## Likely cause

Worth checking before assuming a prompt fix is enough: the `pa` handles reach
the model through a different part of the tick context than the `pc` ones (the
"see above" literature block), and the model may be copying the *presentation*
it sees there. If that block lists papers bare, the prompt rule is competing
with a stronger in-context example and reformatting the rule alone won't hold.
Compare how `pc` vs `pa` handles are rendered into the prompt before editing
the instruction.

## Verify

After a tick on any literature-heavy quest, no bare or parenthesised `pa` /
`pc` / `st` / `fi` handle in the narrative chunk:

```sql
SELECT text FROM chunks WHERE chunk_id = 2856653;
```

Every occurrence should sit inside square brackets.
