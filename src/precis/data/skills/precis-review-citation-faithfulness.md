---
id: precis-review-citation-faithfulness
title: precis — one-pass citation-faithfulness review
summary: For each \citequote in a .tex file, fetch the cited paper's chunks and confirm the verbatim quote actually appears
applies-to: get (kind='tex'|'paper'|'citation'), put (kind='finding')
status: active
---

# precis-review-citation-faithfulness — does the cited paper actually say this?

One review pass. One concern: every `\citequote{key}{quote}` in the
manuscript must have its verbatim `quote` argument appear in the
cited paper's chunks. Paraphrased quotes, quotes from the wrong
paper, and quotes the writer fabricated are all caught here — and
they are the single highest-value finding category in any review.

This is the **citation half** of "does the source support the
claim?" The complementary half — "does the claim actually follow
from this passage?" — lives in precis-review-paper-help under
verifier-loop. Run that separately; here we focus on the
mechanical question first because it's the cheapest catch and
strongly correlates with sloppy writing.

## The procedure

For each `\citequote{key}{quote}` in body text:

1. Resolve `key` to a paper ref. Convention: `key` is the
   paper's slug (e.g. `javey2003`) or DOI. `get(kind='paper', id=key)`
   either returns it or raises NotFound — a NotFound is itself a
   finding (cite to non-corpus paper, see
   precis-doi-extract-help for the fix).
2. Search the paper's chunks for the verbatim quote:
   `search(kind='paper', q='<quote>', scope='<slug>')`. Hybrid
   lexical+semantic; a near-exact match wins.
3. If the quote appears verbatim — done, no finding.
4. If a paraphrased variant appears — finding: paraphrased quote.
   Quote both the .tex argument and the paper's actual passage.
5. If nothing matches in this paper but the same quote appears in
   a *different* corpus paper — finding: wrong cite key.
6. If nothing matches anywhere — finding: fabricated quote. This
   is the highest-severity finding type.

## Output: one finding per problem

Mint `kind='finding'` refs linked to the manuscript ref and the
cited paper. Each finding's body carries the precise diff so a
single re-tick on the writer can fix it.

```python
put(kind='finding',
    text='''Citation drift in chapters--results~kinetics block 7:

\\citequote{collins06}{we observed 12% Faradaic efficiency...}

collins06's actual chunk @ collins06~14 reads:
"a Faradaic efficiency of approximately 10% was measured"

Severity: SUBSTANTIVE — the quote argument is paraphrased AND the
number is changed (12% vs ~10%). The claim's quantitative core
breaks.''',
    link='paper:collins06',
    rel='cited-without-faithful-quote')
```

Findings stay open until the writer's next tick resolves them.
The `all_child_findings_resolved` auto_check evaluator (T3.1)
closes the parent review-pass todo only when every finding is
either closed (STATUS:done by the writer) or won't-do.

## What counts as "verbatim"

Verbatim is exact text, ignoring trivial typographic differences:

- Smart quotes vs straight quotes — equivalent.
- Single vs double-spaced sentence breaks — equivalent.
- Line-wrap hyphens (`hap-pened`) joined or split — equivalent.
- Whitespace around punctuation — equivalent.

**Not** equivalent:

- Different numbers (12% vs 10% is a SUBSTANTIVE finding even if
  surrounding text matches).
- Different units (mM vs M is the same way).
- Different signs, exponents, ratios.
- "approximately" added or removed (changes claim strength).
- Sentence cuts that drop a qualifying clause.

When in doubt, write the finding. False positives are cheap; a
hallucinated quote that survives review is expensive.

## Anti-patterns

- "Looks similar" — not a verbatim check. Pull the paper chunk.
- Trusting the writer's `\citequote` argument without searching
  the paper. The argument is the *claim* under test.
- Aggregating findings into one "many quotes don't match" — one
  finding per quote so each can be resolved independently.
- Skipping `\citequote` whose `key` resolves to a paper that
  isn't in corpus. That IS a finding — the cite cannot be
  verified, which is itself a substantive flaw.

## See also

```python
get(kind='skill', id='precis-citation-help')          # write side of citations
get(kind='skill', id='precis-tex-help')               # \citequote macro
get(kind='skill', id='precis-review-paper-help')      # full adversarial review including claim-support
get(kind='skill', id='precis-common-reviewer')        # shared reviewer discipline
get(kind='skill', id='precis-finding-help')           # how to write a finding
```
