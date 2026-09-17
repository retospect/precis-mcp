---
id: precis-check-source-help
title: precis — find a passage, read its surrounds, check it supports the point
summary: reader-side source-checking — locate the passage by chunk handle, fetch the chunks around it (~A..B range), judge whether it actually supports the claim, then ground a finding hub on it and cite the hub [fi<id>]
answers:
  - how do I find the passage in a paper that backs a claim I want to cite?
  - how do I read the context around a chunk before citing it?
  - how do I judge whether a quote actually supports the claim I'm making?
  - how is reviewing someone else's citation different from finding my own?
applies-to: get/search (kind='paper'), drafting prose, put (kind='finding')
status: active
tags: [drafting]
kinds: [paper, finding]
---

# precis-check-source-help — does the source actually support the point?

Before you cite a passage — or when you're reviewing one someone else
cited — you do three things: **find** the passage, **read its
surrounds**, and **judge** whether it really supports the claim. Then
you ground a finding hub on it and cite the hub `[fi<id>]` — the
chunk is never the cite. This is the reader side; the write side (hub
search → mint → adversarial check → cite) is [[precis-citation-help]].

The failure this prevents: a quote that looks supportive in isolation
but is hedged, negated, or about a different system once you read the
sentence before and after it. A citation that survives this check
survives review.

## How do I find a citation / the passage that backs a claim?
## Where in this paper does it say X?
## Locate the supporting sentence for my claim

Scope the search to the one paper and query the claim:

```python
search(kind="paper", q="Faradaic efficiency CO2 reduction", scope="<slug>")
search(kind="paper", q="<claim or key phrase>", scope="<slug>", page=2)
```

Restricted to that paper's blocks (mechanics: `precis-search-help`).
Each hit is a chunk handle `pc<chunk_id>` — that's your anchor. Rare tokens
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
get(id="pc7")  # the hit itself, one block
get(kind="paper", id="<slug>~5..9")  # blocks 5–9: the hit + its surrounds
get(kind="paper", id="<slug>~38")  # a single block by position
```

If the search returned `pc7` and you don't know its position, open the
paper's TOC to place it, then drill the range:

```python
get(kind="paper", id="<slug>", view="toc")  # the reading map
get(kind="paper", id="<slug>~63..89")  # drill a TOC range
get(kind="paper", id="<slug>~63..89", view="toc")  # sub-TOC of a range
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

If it holds → cite it inline (next section). If it doesn't → keep
looking (another chunk, another paper), or, for an empirical claim
worth chasing to its origin, register a `kind='finding'` and cite it
`[fi<id>]` meanwhile ([[precis-finding-help]]). Don't cite a passage
that only "looks similar" — pull it and read it.

**A fifth check: is this passage the source, or hearsay?** A hit in a
review's summary, a related-work section, or an introduction citing
someone else's result supports the point but isn't *the* source — walk
back to the paper that actually did the work
([[precis-cite-paper-help]]'s primary-source policy). A hanging claim
beats a hearsay cite.

## I confirmed the passage supports the claim — now what?
## Ground a hub on the chunk, cite the hub

The verified chunk is a hub's **grounding**, never the cite itself.
Search for a hub that asserts the claim, attach the chunk to it or mint
one on it, run the adversarial check, then write the hub's handle:

```python
search(kind="finding", q="<the claim>", status="*", mode="semantic")
link(kind="finding", id="fi<id>", rel="corroborates", target="pc7")  # hub exists
put(kind="finding", title="<one self-contained claim sentence>",
    supporters=[{"paper": "pa<id>", "source_handle": "pc7"}])  # no hub yet
```

```text
Aqueous synthesis yields higher quantum yields than hot-injection [fi41].
```

The `source_handle` is the chunk you just read and verified — copy it
from the search / get output, never construct it. Export resolves each
`[fi<id>]` → its originator paper(s) and emits one bibliography entry
per paper; you never type LaTeX citation commands. The full procedure,
including the adversarial search: [[precis-citation-help]].

## Triage excerpts are not citation-grade

Search results carry short `excerpt @ ~N:` sub-lines picked for triage
— they help you decide whether to drill in, but they are clipped. Never
judge support from the excerpt: fetch the full chunk (`get(id='pc<id>')`
or the `~A..B` range) and read it before you cite.

## Reviewing someone else's citation (the faithfulness check)

Checking that each inline `[fi<id>]` in a manuscript resolves to a
hub whose grounding chunks support the claim it sits beside is the same skill at
scale — one finding per unsupported or dangling handle. That review
pass is [[precis-review-citation-faithfulness]].

## See also

- [[precis-citation-help]] — write side: hub search → mint → adversarial check → `[fi<id>]` cite.
- [[precis-cite-paper-help]] — the cite-a-paper router.
- [[precis-paper-help]] — `~A..B` grammar, TOC, scoped search.
- [[precis-finding-help]] — chase a claim to its primary source.
- [[precis-review-citation-faithfulness]] — batch faithfulness review.
- [[precis-search-help]] — search mechanics, excerpt vs chunk.
