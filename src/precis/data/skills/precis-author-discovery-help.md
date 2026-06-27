---
id: precis-author-discovery-help
title: precis — author-network discovery (BFS)
summary: Grow the corpus by walking paper → author → paper — resolve senior authors via ORCID and enqueue their missing work
applies-to: get/search (kind='semanticscholar', kind='orcid')
status: active
---

# precis-author-discovery-help — author-network discovery

Author-network BFS is a **discovery strategy you drive**, composed from
the basic `orcid` + `semanticscholar` interfaces — not a built-in
traversal. The thesis: on a good paper, the **senior (last) author** is
usually the lab PI / subject-matter expert, and their back catalogue is
the densest vein of related key work — including work a citation-graph
walk misses (unrelated-by-citation output from the same group, and
pre-prints not yet cited). Resolving them lets you pull that whole vein
into the fetch queue. Senior-author-first is **one heuristic among
several** (see Frontier scoring) — you choose whom to expand.

## The loop

```python
# 1. From a high-value paper, list its authors. The last author is
#    flagged "senior"; each row carries the author's ORCID + S2 id.
get(kind='semanticscholar', id='authors:10.1038/nature12373')

# 2. Resolve the senior (then co-) authors by their ORCID iD. This
#    stores the author node, links the works you already hold, and
#    REPORTS how many are missing — it does not auto-fetch.
get(kind='orcid', id='0000-0002-1825-0097')

# 2b. Opt in to fetching: enqueue as many of the missing works as the
#     frontier budget allows (or 'all'). LLM-gated — see precis-orcid-help.
get(kind='orcid', id='0000-0002-1825-0097', args={'enqueue': 20})

# 3. The fetch_oa worker grabs open-access PDFs for the new stubs
#    out-of-band. Read what landed, score the frontier, expand.
search(kind='paper', q='your topic')

# 4. Widen the frontier with the author's top S2 papers (each row's
#    DOI feeds another put(kind='paper', doi=...) / authors: hop).
get(kind='semanticscholar', id='author:1741101')
```

## Frontier scoring

Don't BFS blindly. After step 2, score candidate authors / papers by:

- **shared affiliation** (ROR id matches a group you're tracking),
- **shared venue** (same journals/conferences as your core corpus),
- **recency** (recent senior-author output > decade-old first-author).

Expand to depth 1–2 and **stop on a budget** — you control the spend
directly via the `enqueue=N` count per author (step 2b); the
`set_by='orcid'` attribution keeps every minted stub traceable. A
runaway walk at worst enqueues stubs you asked for, never blows a
download budget inline.

## Why two sources

- **Semantic Scholar** (`authors:` / `author:`) is the *navigation*
  layer — it gives you the author list, the senior flag, h-index,
  affiliations, and the ORCID iDs to hop on. It's fuzzy on identity.
- **ORCID** (`kind='orcid'`) is the *identity + completion* layer — a
  disambiguated, complete works list, and the durable `authored` link
  hub. Confirm via the iD before treating an author as canonical.

## As a planner coroutine

This loop is a natural `LLM:*` planner tick: each tick resolves one
author, the stubs fetch out-of-band, the next tick reads what landed
and picks the next frontier node. Run it by hand first to validate the
scoring heuristic, then promote to a recurring planner.

## See also

```python
get(kind='skill', id='precis-orcid-help')          # the ORCID node + LLM-gated enqueue
get(kind='skill', id='precis-stubs-help')          # the stub → fetch pipeline
get(kind='skill', id='precis-decomposition-help')  # when to split / block / wait
```
