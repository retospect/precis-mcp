---
id: precis-review-citation-faithfulness
title: precis — one-pass citation-faithfulness review
summary: For each [pc<id>] citation in a draft, resolve the cited paper chunk and confirm it actually supports the claim
applies-to: get (kind='draft'|'paper'), put (kind='finding')
status: active
---

# precis-review-citation-faithfulness — does the cited chunk actually say this?

One review pass. One concern: every inline citation `[pc<id>]` in the
manuscript must point at a paper chunk that **actually supports the
claim it backs**. The handle resolves to one exact passage — pull it
and read it. Citations to a chunk that doesn't support the claim,
citations to the wrong chunk, and handles the writer guessed instead
of copying are all caught here — and they are the single
highest-value finding category in any review.

This is the **citation half** of "does the source support the
claim?" The complementary half — "does the claim actually follow
from this passage?" — lives in precis-review-paper-help under
verifier-loop. Run that separately; here we focus on the
mechanical question first because it's the cheapest catch and
strongly correlates with sloppy writing.

## The procedure

For each inline citation handle (`[pc<id>]` paper chunk, `[pk<id>]`
patent, `[fi<id>]` finding) in body text:

1. Resolve the handle to the exact chunk: `get(id='pc<id>')`. It
   either returns the chunk or raises NotFound — a NotFound is itself
   a finding (the handle was guessed, not copied from search/get
   output; see precis-doi-extract-help for the acquisition fix).
2. Read the chunk's text and compare it against the claim the
   citation backs in the draft.
3. If the chunk directly and substantively supports the claim —
   done, no finding.
4. If the chunk is topically related but only weakly supports a
   softened claim — finding: weak / inflated citation. Quote both the
   draft claim and the chunk's actual passage.
5. If the chunk supports a *different* claim, or the writer cited the
   wrong paper for this one — finding: wrong cite.
6. If the chunk says nothing that bears on the claim — finding:
   unsupported claim. This is the highest-severity finding type.

A citation is the **bare paper-chunk handle written inline** —
`[pc234]`, or several supporting chunks `[pc232][pc234][pc593]`. The
author never types `\cite{}`; that is export-only output. A
`[me<id>]`/`[dc<id>]` reference is a **link, not a citation** (it
points at our own notes, not the literature) — it is out of scope
here; skip it.

## Output: one finding per problem

Mint `kind='finding'` refs linked to the manuscript ref and the
cited paper. Each finding's body carries the precise diff so a
single re-tick on the writer can fix it.

```python
put(kind='finding',
    text='''Citation drift in dc207 (Results > Kinetics):

The claim "we observed 12% Faradaic efficiency..." cites [pc1843].

pc1843's actual text reads:
"a Faradaic efficiency of approximately 10% was measured"

Severity: SUBSTANTIVE — the cited chunk supports ~10%, not 12%. The
claim's quantitative core breaks.''',
    link='pc1843',
    rel='cited-without-support')
```

Findings stay open until the writer's next tick resolves them.
The `all_child_findings_resolved` auto_check evaluator (T3.1)
closes the parent review-pass todo only when every finding is
either closed (STATUS:done by the writer) or won't-do.

## What counts as "support"

Support is the cited chunk establishing the claim's substantive core.
Trivial wording differences between claim and chunk are fine — the
chunk does not have to echo the sentence. What breaks support:

- Different numbers (claim says 12%, chunk says 10% — a SUBSTANTIVE
  finding even if the surrounding text matches).
- Different units (mM vs M is the same way).
- Different signs, exponents, ratios.
- "approximately" present in the chunk but dropped in the claim
  (changes claim strength → citation inflation).
- The claim asserts what the chunk only suggests / is consistent
  with.

When in doubt, write the finding. False positives are cheap; an
unsupported citation that survives review is expensive.

## Anti-patterns

- "Looks similar" — not a support check. Pull the chunk with
  `get(id='pc<id>')` and read it.
- Trusting the handle without resolving it. The cited chunk is the
  *evidence* under test.
- Aggregating findings into one "many cites don't hold" — one finding
  per citation so each can be resolved independently.
- Treating a `[me<id>]`/`[dc<id>]` link as a citation. Those point at
  our own notes (a `related-to` link), never the literature — they
  are not in scope here and never reach the bibliography.
- Skipping a `[pc<id>]` whose handle resolves to nothing. That IS a
  finding — the handle was guessed instead of copied, so the cite
  cannot be verified, which is itself a substantive flaw.

## See also

```python
get(kind='skill', id='precis-draft-help')             # write side: inline [pc<id>] citations
get(kind='skill', id='precis-bibliography-help')      # read side: who cites a paper
get(kind='skill', id='precis-review-paper-help')      # full adversarial review including claim-support
get(kind='skill', id='precis-common-reviewer')        # shared reviewer discipline
get(kind='skill', id='precis-finding-help')           # how to write a finding
```
