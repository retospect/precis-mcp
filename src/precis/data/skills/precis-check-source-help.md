---
id: precis-check-source-help
title: precis — find a citation, read its surrounds, check it supports the point
summary: reader-side source-checking — locate the passage, fetch the chunks around it (~A..B range), judge whether it actually supports the claim, then persist
applies-to: get/search (kind='paper'), put (kind='citation'|'finding')
status: active
---

# precis-check-source-help — does the source actually support the point?

Before you cite a passage — or when you're reviewing one someone else
cited — you do three things: **find** the passage, **read its
surrounds**, and **judge** whether it really supports the claim. This
is the reader side; the write side (persisting the verified pair) is
[[precis-citation-help]].

The failure this prevents: a quote that looks supportive in isolation
but is hedged, negated, or about a different system once you read the
sentence before and after it. A citation that survives this check
survives review.

## How do I find a citation / the passage that backs a claim?
## Where in this paper does it say X?
## Locate the supporting sentence for my claim

Scope the search to the one paper and query the claim:

```python
search(kind='paper', q='Faradaic efficiency CO2 reduction', scope='<slug>')
search(kind='paper', q='<claim or key phrase>', scope='<slug>', page=2)
```

Hybrid lexical + semantic, restricted to that paper's blocks. Each hit
is a chunk handle `pc<chunk_id>` — that's your anchor. Rare tokens
(a compound name, a number, a DOI) rank high, so quote the most
distinctive phrase from the claim. No paper in mind yet? Drop `scope=`
to search the whole corpus, or see [[precis-paper-help]].

## How do I check a chunk's surrounds / read the context around it?
## Read the sentences before and after a passage
## Get the surrounding paragraphs of a chunk

**This is the step most agents miss.** A search hit is one block; the
claim's real meaning lives in the blocks around it. Use the **range
selector** `~A..B` — *not* `view='chunks'`, *not* `args={'chunk_range':…}`
(those don't exist):

```python
get(id='pc7')                              # the hit itself, one block
get(kind='paper', id='<slug>~5..9')        # blocks 5–9: the hit + its surrounds
get(kind='paper', id='<slug>~38')          # a single block by position
```

If the search returned `pc7` and you don't know its position, open the
paper's TOC to place it, then drill the range:

```python
get(kind='paper', id='<slug>', view='toc')   # the reading map
get(kind='paper', id='<slug>~63..89')        # drill a TOC range
get(kind='paper', id='<slug>~63..89', view='toc')  # sub-TOC of a range
```

Read a few blocks on each side. You're checking for a leading
"however", a "we did *not* observe", a different sample or condition,
or a hedge ("approximately", "under idealised assumptions") that the
isolated sentence hid.

## Does this passage actually support my point?
## Judge whether the quote backs the claim

Hold the passage to the claim and ask:

1. **Polarity** — does it *affirm* the claim, or qualify/negate it?
   A "however" or "in contrast to" before the quote flips its meaning.
2. **Scope match** — same system, conditions, units, sample? "12% FE"
   for a different electrode/electrolyte is a *different* finding, not
   support.
3. **Numbers match** — a "12%" claim needs "12%" (or "twelve percent")
   in the source. "An improvement was noted" does **not** support a
   numeric claim — that's a hallucinated citation. Reject it.
4. **Strength** — does the source hedge ("suggests", "may indicate")
   more than the claim asserts? Match the claim's strength to the
   source's.

If it holds → persist the verified pair (next section). If it doesn't →
keep looking (another chunk, another paper), or, for an empirical claim
worth chasing to its origin, register a `kind='finding'`
([[precis-finding-help]]). Don't cite a passage that only "looks
similar" — pull it and read it.

## I confirmed the passage supports the claim — now what?
## Persist the verified citation

Quote the source **verbatim** from the chunk you read (no paraphrase,
no cleanup) and store the pair:

```python
put(kind='citation',
    text='<the claim>',
    source_handle='pc7',                 # the chunk you verified against
    source_quote='<exact text from the chunk>',
    verifier_confidence=0.95,
    link='paper:<slug>', rel='cites')
```

Verbatim matters because the whole point is that a future reader can
chase to the exact span. Cleanup destroys that. Full write protocol +
the `\citequote` LaTeX bridge: [[precis-citation-help]].

## Triage excerpts are not citation-grade

Search results carry short `excerpt @ ~N:` sub-lines picked for triage
— they help you decide whether to drill in, but they are clipped. Never
verify against the excerpt: fetch the full chunk (`get(id='pc<id>')` or
the `~A..B` range) and read it before judging support.

## Reviewing someone else's citation (the faithfulness check)

Checking that a manuscript's `\citequote{key}{quote}` actually appears
verbatim in the cited paper is the same skill at scale — one finding
per mismatch. That review pass is [[precis-review-citation-faithfulness]].

## See also

```python
get(kind='skill', id='precis-citation-help')             # write side: persist the verified pair
get(kind='skill', id='precis-cite-paper-help')           # the cite-a-paper router
get(kind='skill', id='precis-paper-help')                # ~A..B grammar, TOC, scoped search
get(kind='skill', id='precis-finding-help')              # chase a claim to its primary source
get(kind='skill', id='precis-review-citation-faithfulness') # batch faithfulness review
get(kind='skill', id='precis-search-help')               # search mechanics, excerpt vs chunk
```
