---
id: precis-read-for-question
title: precis — read the corpus for a question and leave claim hubs behind
summary: the question-driven reading loop — hubs first, then papers the hubs do not already rest on, one reader per paper (toc → scoped search → ranges → proposals), root converges proposals onto hubs; returns fi ids labelled new/existing × answers/partial/side
answers:
  - I have a question — which papers should I read and how, so the facts end up as claim hubs?
  - how do I read papers for a question without re-reading what we already hubbed?
  - how do I keep parallel readers from minting near-duplicate hubs?
  - a reader found a useful fact unrelated to my question — mint it or not?
  - how do I turn a cross-paper inference into something citable?
applies-to: search/get (kind='finding'|'paper'), put (kind='finding'|'todo'|'memory'), link
status: active
tags: workflow, drafting
kinds: finding, paper, todo
---

# precis-read-for-question — read for a question, leave hubs behind

You have a question. The corpus has papers. The output you want is not
notes: it is a list of claim hubs (`fi<id>`) that answer the question,
each grounded on the passage that says so, so the next writer cites
`[fi<id>]` instead of reading the paper again. This skill is the loop.
It routes to the skills that own each step and adds only the order, the
guards, and the caps.

One idea governs the whole loop: **the hub is the merge point.** One
world-claim, one hub, many papers attached as evidence. A reader never
mints; it proposes. The root converges proposals onto hubs. That is how
five parallel readers do not produce five hubs for one fact.

## Step 1 — ask the claim layer before the corpus
## Search hubs first

```python
search(kind="finding", q="<the question, as the answer sentence would read>", status="*", mode="semantic")
search(kind="finding", q="<its rarest token: a number, a compound, a phrase>", status="*")
```

`status='*'` is required; the default cohort hides most hubs. Read each
hit's `state`, `support` and `flags` before trusting it
([[precis-citation-help]] step 1 says how). A hit that answers the
question is a result — record it as `existing`. Then keep going: a hub
found is not a reason to stop reading, it is a reason to exclude its
supporters from the next search.

## Step 2 — find papers the hubs do not already rest on
## Search papers with the well-trodden set excluded

The bias to defeat is vocabulary, not ranking. A query for "nanotube"
never lifts the "tubule" paper above the fold; excluding what is already
hubbed or cited is what surfaces the nearby rest. So:

```python
search(kind="paper", q="<question>",
       queries=["<question, second phrasing>", "<the technique you would expect>"],
       answers=["<a sentence that would state the result>"],
       per_paper=2)
search(kind="paper", q="<question>", hubbed=False, per_paper=2)  # skip papers already backing a hub
search(kind="source", q="<question>")  # papers + hubs together, one posture-prefixed table
```

Rules for this step:

- **Second pass carries harvested vocabulary.** Take the terms the
  first pass's hits and the step-1 hub sentences use and put them in
  `queries=`. There is no automatic synonym expansion; you are it.
- **Exclude the well-trodden set.** `hubbed=False` drops every paper
  that's already a supporter of a live claim hub — no hand-built
  `exclude=` list needed. `kind='source'` is the mirror move: the same
  question fanned out over papers and hubs together, each hub row
  carrying its posture (`◆ 4✓ unopposed` / `◆ refuted` / `◆ disputed`)
  so a settled answer is visible before you read a single passage
  ([[precis-search-help]]).
- **Facet tags bridge what the query cannot.** A `material:` or
  `domain:` axis filter with a loose query reaches papers no phrasing
  ranks ([[precis-paper-tag-axes]]). `tags=['studytype:review']` is the
  set to *skip* unless you want a synthesis claim: a review is
  secondhand by genre and cannot ground a hub.
- **Stop at a reading list, not a pile.** Five to eight papers per pass
  is the useful size; more than that means the question needs splitting
  ([[precis-decomposition-help]]).

## Step 3 — one reader per paper
## What a reader does

Each paper is a todo leaf under the question's todo, `meta.llm_tier='sonnet'`.
The leaf's `claimed-by:` lease is the only lock: nothing else stops two
readers on one paper, and nothing needs to, because every write below is
idempotent and the root dedups. The reader:

1. `get(kind='paper', id='pa<id>', view='toc')` for the section map. If
   the header reports the paper is not yet embedded, use `mode='lexical'`
   inside it, or park the leaf until it is (embedding lands from the
   worker, not from you).
2. One scoped search per facet of the question. This is the guard
   against the qualifying paragraph you would otherwise miss in methods
   or supplementary:

   ```python
   search(kind="paper", q="<facet of the question>", scope="pa<id>")
   get(kind="paper", id="<slug>~A..B")  # the hit plus its surrounds
   ```

3. Read whole only when the toc shows fewer than about twenty body
   chunks. Otherwise results, tables, conclusions; never the
   introduction or related-work block as grounding
   ([[precis-check-source-help]]).
4. Propose, do not mint. A proposal is `{sentence, scope, grounding
   chunk handles}` written to the mint skill's admissibility test:
   falsifiable, self-contained, method-attributed, one assertion, the
   source's precision ([[precis-taproot-mint-help]]).
5. A review paper returns the primaries' DOIs it attributes the result
   to, not proposals. The root stubs those (`put(kind='paper', doi=…)`)
   or registers a chase finding ([[precis-finding-help]]).
6. Return proposals in the leaf's result, tagged `answers`, `partial` or
   `side`.

## Step 4 — the root converges proposals onto hubs
## Mint, attach, or refine

For each proposal the root calls the hub door:

```python
put(kind="finding", title="<sentence>", scope={"<regime-key>": "<term>"},
    supporters=[{"paper": "pa<id>", "source_handle": "pc<chunk>"}])
```

On a build where the door runs the semantic dedup itself, the response
says whether it minted, converged onto an existing hub, or minted and
filed a review todo naming the nearest hub — act on that line. On an
older build the root does the judging by hand first:
[[precis-citation-help]] step 1 (sentence, then rarest token); same
claim, same scope → `link(kind='finding', id='fi<id>', rel='corroborates',
target='pc<chunk>')` and record `existing`; sharper or narrower → mint,
then `link(rel='refines')` to the coarser hub; a number that disagrees →
mint, then `link(rel='disputes')` ([[precis-taproot-hub-edit-help]]).

Identical wording twice is not how convergence happens; the judge over
nearest hubs is. Trust it, and let the human approve queue be the last
line ([[precis-nanopub-help]]).

## Step 5 — side-facts: cap them
## The paper had a useful fact unrelated to the question

Every minted hub is an approve click for a human later. The rule:

- **Cap at two side-fact hubs per paper**, and only if each is
  quantified and admissible. A definition, a "was investigated", a
  bibliography line: not a hub, not anything.
- **Not hub-worthy means not artefact-worthy.** There is no digest to
  write; the paper's claims view already lists what it grounds. The one
  exception is a non-claim note worth keeping ("supplementary has the
  raw table; the main text rounds it"), which is a `memory` linked to
  the paper ([[precis-memory-help]]).
- **Support before mint.** A side-fact that an existing hub already
  asserts is a corroborating passage, not a new hub, and corroboration
  costs no approval.

## Step 6 — cross-paper inference
## A from one paper plus B from another suggests C

That is not a claim; it has no grounding passage, and hand-bundling
atoms into a compound is forbidden. It is a hypothesis:

```python
put(kind="finding", hypothesis=True, title="<declarative sentence>",
    motivation="<what A and B each established; which transfer is unproven>",
    testable_by="<the measurement that would settle it>",
    motivated_by=["fi<a>", "pc<b>"], llm_models=["<your model id>"])
```

One pass over the new and existing ids after all readers return, its
own cap (one or two per question), rules in [[precis-nanopub-help]].

## What the root returns
## The deliverable

A list of `fi<id>`, each labelled `new` or `existing`, and `answers`,
`partial` or `side`. If the question belongs to a quest, attach the hubs
to it so the serves-graph shows them ([[precis-quest-help]]). The
question's todo closes with that list as its result; the reader leaves
carry the per-paper proposals as their log.

## See also

- [[precis-citation-help]] — the four-step cite procedure this loop feeds.
- [[precis-cite-paper-help]] — the router when you are writing, not reading.
- [[precis-research-help]] — the quality bar for any research pass.
- [[precis-taproot-mint-help]] — admissibility, grounding, review-source rule.
- [[precis-taproot-help]] — what a hub is and how `[fi<id>]` resolves.
- [[precis-search-help]] — `queries=`, `answers=`, `per_paper=`, `exclude=`, `uncited=`.
- [[precis-paper-help]] — toc, summaries, ranges, scoped search.
