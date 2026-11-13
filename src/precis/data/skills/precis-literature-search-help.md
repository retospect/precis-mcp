---
id: precis-literature-search-help
family: paper
title: precis — the end-to-end literature search procedure
summary: the order of moves across kinds — skill lookup, memory first, broad hybrid paper search, refine-don't-page, verify and widen on a hit, thin-corpus escalation (cache checks → websearch → perplexity-reasoning → perplexity-research → Semantic Scholar/websearch for primary DOIs → ingest → gap entry), write-back to memory
answers:
  - how do I run a literature search for papers on a topic, start to finish?
  - what order do I search memory, the corpus, Semantic Scholar and Perplexity in?
  - what do I do when the corpus is too thin on my topic?
  - where do Semantic Scholar and the Perplexity tiers sit in the escalation order?
  - how do I keep the next literature search on this topic from starting cold?
applies-to: search/get/put (kind='skill'|'memory'|'paper'|'source'|'semanticscholar'|'websearch'|'perplexity-reasoning'|'perplexity-research')
status: active
tags: [workflow, external-sources]
kinds: [paper, memory, semanticscholar, websearch, perplexity-reasoning, perplexity-research]
---

# precis-literature-search-help — one literature search, end to end

The moves for "find me the papers on X" live in half a dozen skills —
search mechanics in [[precis-search-help]], the quality bar in
[[precis-research-help]], Semantic Scholar walks in
[[precis-paper-help]], Perplexity tiers in [[precis-perplexity-help]].
This skill holds the order you play them in; it adds no mechanics of
its own. Work it top to bottom — each step names the skill that owns
the detail.

## The order at a glance
## What is the first move in a literature search?

1. **Skills** — `search(kind='skill', q='<task>')`: a domain playbook may
   already exist.
2. **Memory** — `search(kind='memory', q='<topic>')`: prior corpus maps,
   rulings and cached external runs.
3. **Corpus** — broad hybrid `search(kind='paper', …)`; cross-kind or
   `kind='source'` when claim hubs should surface too.
4. **Refine** — more hits than a page: narrow, never page.
5. **Verify and widen** — read the hit's chunk, then
   `extent='fisheye+1hop+recall'` for its neighbourhood.
6. **Escalate** — thin corpus: cache checks → websearch →
   perplexity-reasoning → perplexity-research → Semantic Scholar/websearch
   for primary DOIs → ingest → an explicit gap entry.
7. **Write back** — the corpus map, ruling or gap becomes a memory, so
   step 2 finds it next time.

## Steps 1–2 — skills, then memory, before spending calls
## Has someone already thought about this topic?

```python
search(kind="skill", q="literature search for <the task>")
search(kind="memory", q="<the topic>")
```

A domain playbook beats re-deriving the procedure. Memory-first is the
cheap win: prior corpus maps, rulings and earlier Perplexity reports
live as memories, so a second literature search on a topic finds the
first one's harvest. It only works when step 7 ran — write-back is
part of the procedure, not polish.

## Step 3 — broad hybrid search over the corpus
## Search papers on the topic, wide net first

```python
search(
    kind="paper",
    q="<the question>",
    page_size=30,
    args={
        "queries": ["<rephrasing>", "<a broader framing>", "<a narrower one>"],
        "answers": ["<a passage an ideal source would contain>"],
    },
)
```

`queries=` rephrases the question, `answers=` (HyDE) supplies a fake
ideal passage — the fused list surfaces gold a single phrasing buries.
`mode='lexical'` instead when you hold an exact string (an identifier,
an acronym, a numeric like `1.523 eV`). Omit `kind=` for the
cross-kind fan-out, or aim `kind='source'` when a settled claim hub
should rank alongside raw passages. Knobs and the `good=True`
background campaign: [[precis-search-help]] → Broad retrieval.

## Step 4 — more hits than a page: refine, don't page
## What do I do with '12 of 87 papers'?

Treat the result as one page — there is no page 2 in this loop. A
`Next:` block may still offer a `page=` call; that hint is tracked as
misleading (gr634204), so narrow instead:

- tighten `q=` with a salient term from a `more`-heavy row's keywords;
- `scope='pa…'` into the promising paper — it flips to one row per
  chunk inside it;
- open that paper's keyword-labelled TOC
  (`get(id='pa…', view='toc')`) and read only the ranges whose labels
  match the topic (gr634201);
- or re-issue the query more specific — or less specific, when your
  wording was the thing hiding the gold.

The dated source search (`sort=` top-level, `since=`/`until=` in
`args=`) is the one path that genuinely paginates —
[[precis-search-help]] → Refine a broad result set.

## Step 5 — verify a hit, then widen from it
## How do I check a hit and find its neighbours?

```python
get(id="pc…")                                  # the full chunk
get(id="pc…", extent="fisheye+1hop+recall")    # chunk + linked refs + near-misses
```

Cite only after reading the chunk — never off a keyword row. Once a
hit is good, widen around it: `fisheye+1hop` shows the claims and
memories already linked to that passage, `+recall` adds the nearest
unlinked chunks — the almost-matches the ranking missed. The extent
ladder: [[precis-fisheye-help]].

## Step 6 — thin corpus: escalate by purpose
## Which external rung do I reach for, in what order?

The corpus is thin when a sub-topic has under ~3 citable sources, or a
survey under ~10 ([[precis-research-help]]). Escalate in this order —
the Perplexity rungs cost more as you climb:

**(a) Check the paid-kind caches before paying.** A timed-out
`perplexity-research` call may already have landed in the cache, and
`/recent` never spends:

```python
search(kind="perplexity-research", q="<the topic>")
get(kind="perplexity-research", id="/recent")   # also websearch / perplexity-reasoning
```

**(b) `websearch` (~$0.001, 2–5 s)** — the cheapest paid rung: a single
fact, or the first landscape pass that names the papers worth chasing.

**(c) `perplexity-reasoning` (~$0.005, 5–30 s) when the vocabulary or
landscape is unknown** — it names the terms, groups and debates the
corpus search then re-queries by.

**(d) `perplexity-research` last** — survey-grade only: minutes per
call, up to ~$3 on a long prompt. The cache key includes the tier, so
each escalation is a fresh paid call; a Pro web-UI answer imports the
same row free via `put(mode='import')`. `id=`/`q=` on the Perplexity
kinds is the *query text*, not a ref id — a digits-only query is
refused. Tiers and gotchas: [[precis-perplexity-help]].

**(e) Resolve the primary DOIs the answer points at — Semantic Scholar
or `websearch`.** Semantic Scholar returns DOIs flagged `held:` /
`stub:` / `NEW` against the corpus, so you buy only what's missing:

```python
get(kind="semanticscholar", id="<title or topic>")      # ranked hits + DOIs
get(kind="semanticscholar", id="refs:<doi>")            # a held paper's bibliography
get(kind="semanticscholar", id="cites:<doi>")           # its forward citations
put(kind="paper", doi="<the NEW hit's doi>")            # mint the stub
```

`refs:` of a held review is the high-precision route to the missing
primary source. The `fetch_oa` worker pulls open-access PDFs
out-of-band; a later `search(kind='paper', …)` reads what landed.
Cached 30 days, 50 rows per hop; the per-call cost isn't stated in
[[precis-paper-help]] — check before a bulk walk.

Then ingest the primaries and cite them — not Perplexity and not the
review that pointed at them. Still missing → an explicit gap entry
("no papers on X under condition Y") is worth more than dressed-up
prose.

## Step 7 — write back so memory-first has something to find
## Where does the result of a literature search live?

Save the harvest as a memory — the corpus map (what's held, what's
stubbed), the ruling the search settled, or the gap it confirmed.
Without it step 2 finds nothing and the next search starts cold. The
write mechanics and shape rules: [[precis-make-memory]],
[[precis-memory-help]].

## See also

- [[precis-search-help]] — search mechanics, modes, broad retrieval
- [[precis-research-help]] — the quality bar the findings must meet
- [[precis-read-for-question]] — the reading loop that leaves claim hubs
- [[precis-paper-help]] — paper views, Semantic Scholar walks, stubs
- [[precis-perplexity-help]] — tier costs, cache keys, import mode
- [[precis-fisheye-help]] — extent ladder for verify-and-widen
- [[precis-stubs-help]] — the stub → fetch_oa acquisition pipeline
- [[precis-author-discovery-help]] — author-walk expansion once a seed is held
