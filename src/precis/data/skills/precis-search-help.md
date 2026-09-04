---
id: precis-search-help
title: precis — the search verb (mechanics, pagination, filters)
summary: hybrid lexical and semantic search — pagination, tag filters, scope, exclude, cross-kind fan-out
answers:
  - how do I search for exact text instead of a fuzzy match?
  - how do I filter search results by tag?
  - how do I search inside just one ref instead of the whole corpus?
  - how do I run a broad, high-recall search across many papers?
  - how do I paginate through search results?
  - why doesn't my query find a claim I know exists?
  - do I have to type kΩ / µ / Å, or will ASCII work?
  - how do I exclude everything a draft already cites from a search?
  - how do I search for sources a draft hasn't cited yet?
applies-to: search (every kind that supports it)
status: active
---

# precis-search-help — search across kinds

Hybrid lexical + semantic search. Returns ranked handles (`pc<chunk_id>`,
e.g. `pc40`) you paste straight into `get(id=…)` to drill in — the handle's
prefix infers the kind. Order is the relevance signal — for a fused
hybrid or cross-kind result there is no honest numeric score to sort
by. `kind='conv'` is the one exception: it's lexical-only (no
semantic leg yet), so its hits carry a real `(score=0.1234)` — a raw
lexical rank, comparable within that one response, not a probability
or a cross-kind-comparable number, and not a general search contract.

## What knobs does search have?
## Quick reference for search arguments
## How do I call search?

```python
search(q="photocatalysis")  # fan out across all kinds
search(kind="paper", q="photocatalysis")  # one kind
search(kind="paper,patent", q="photocatalysis")  # several kinds
search(kind="paper", q="X", page=2, page_size=20)  # paginate
search(kind="paper", q="X", tags=["topic:noxrr"])  # tag-filter
search(kind="paper", q="X", scope="pa5")  # search inside one ref, by handle
search(kind="paper", q="X", exclude=["pa5", "pa12"])  # skip refs by handle
search(kind="paper", q="X", uncited="dr173020")  # skip what that draft already cites
search(kind="patent", q="X", reach="remote")  # patent/edgar-only knob
search(kind="paper", q="1.523 eV", mode="lexical")  # exact string, no embedding
search(
    kind="paper",
    q="X",
    queries=["rephrase 1", "rephrase 2"],
    answers=["a passage an ideal source would contain"],
    per_paper=2,
    page_size=30,
)  # broad / high-recall (see below)
```

A few more args, in brief: `title=`/`author=` run a byline-record
lookup on paper search (`precis-paper-help`). `folder=` restricts hits
to one folder's live subtree — id, `folder:N`, `fo<N>` handle, or name
— and forces the cross-kind fan-out (`precis-folder-help`).
`angle=`/`like=` run a salience-rotation search seeded from a ref
handle (`precis-dreaming-help`). `view='dreamable'` / `'stubs'` /
`'chase-queue'` swap in a different result shape and ignore `q=` — a
salience pick, or the paper-acquisition backlog (`precis-dreaming-help`,
`precis-stubs-help`). `status=` is shorthand for
`tags=['STATUS:<value>']` on kinds with that axis — see "Enumerating a
tag" below for the per-kind defaults.

## Ranking mode — hybrid (default), lexical, semantic, or verbatim

By default `search` is **hybrid**: a lexical pass fused with a semantic
pass into one ranked list — the right default for "find me things about
X". Pin the ranking with `mode=` when you know better:

| `mode` | What it does | Reach for it when |
|---|---|---|
| `'hybrid'` *(default)* | Lexical + semantic, fused into one order. | General recall — concepts *and* keywords. |
| `'lexical'` | Full-text match only; no embedding. | You know the **exact string** — an identifier, acronym, surname, code token, a numeric like `1.523 eV`, or an exact phrase. Embeddings blur these; lexical is precise and deterministic. Also the honest tool when the embedder is down (hybrid silently degrades to this anyway). |
| `'semantic'` | Embedding-similarity match only. | Pure conceptual / paraphrase recall where the wording won't match but the meaning does. No embedder wired → degrades to lexical; embedder wired but failing → a loud error, never a silent zero-hit answer. |
| `'verbatim'` | Chunks whose extracted keywords contain **all** your query words (exact containment; embedder-independent). No relevance gradient — newest chunk first. | A topical filter tighter than full-text — chunks already keyword-tagged with your term(s). Each word must appear as a *distinct* keyword, so it's terms, not phrases (`'oxygen evolution'` = both words present, not the 2-gram). Empty query returns nothing. |

```python
search(kind="paper", q="MoS2 monolayer", mode="lexical")  # exact term recall
search(q="ways to stop catalyst poisoning", mode="semantic")  # paraphrase recall
search(
    kind="paper", q="perovskite stability", mode="verbatim"
)  # chunks keyworded BOTH terms
```

`mode=` works on a single kind and across the cross-kind fan-out.
Scores are never comparable *across* modes — within one result list,
more-relevant is always first. There is no `mode='regex'` on this verb
(only `kind='draft'` has it — `precis-draft-help`); for a literal or
pattern hunt elsewhere use `'lexical'` or `'verbatim'`.

## The corpus stores one spelling per quantity

ASCII queries are auto-canonicalized — `40 kOhm` finds a claim written
`40 kΩ`, so you don't have to type µ/Å/± by hand. Full canon rules:
`precis-notation-canon`.

## Broad retrieval — when the gold hides behind the wording
## Find more, better, more-diverse chunks for one question
## High-recall paper search (multi-query + HyDE)

A single phrasing is fragile — the best chunk often loses just because
it words the idea differently than you did. Hand `search` **several
angles at once** and let it fuse them; a chunk that surfaces across
phrasings rises to the top.

Two knobs, both paper-side, both fused with `q` into one ranked list:

- `queries=[…]` — **rephrasings of the question** (synonyms, broader /
  narrower framings, sub-questions hiding inside it). Up to 8.
- `answers=[…]` — **hypothetical answer passages** (HyDE): 1–3 short
  paragraphs written the way you'd expect an *ideal source chunk* to
  read. Often the single biggest lever for technical queries — the
  fake answer lives in "chunk space", not "question space". Up to 8.

```python
search(
    kind="paper",
    q="does single-atom Cu help nitrate-to-ammonia selectivity?",
    queries=[
        "single-atom copper catalyst NO3RR selectivity",
        "Cu coordination environment ammonia faradaic efficiency",
        "isolated Cu sites suppress hydrogen evolution nitrate",
    ],
    answers=[
        "Isolating Cu as single atoms on an N-doped carbon support "
        "raises NH3 faradaic efficiency to ~90% by weakening *NO "
        "binding and suppressing the competing hydrogen-evolution "
        "reaction, shifting selectivity toward ammonia.",
    ],
    per_paper=2,  # at most 2 chunks per paper → broader spread
    page_size=30,  # widen the net so the fused set surfaces
)
```

Then **poke around** before trusting a hit: read the full chunk
(`get(id='pc…')`), or `search(kind='paper', scope='pa…', q='…')` to
read more of that paper around it. Cite or write a memory once you've
confirmed the context — never off the keyword row alone.

Rules of thumb:
- Research / triage questions, not exact-string lookups (`mode='lexical'`
  for those).
- 3–5 `queries` + 1–2 `answers` is plenty; more legs ≠ better.
- `per_paper=2` is a good default for *breadth*; drop it to mine one
  paper deeply.
- Honors `mode=`, `tags=`, `scope=`, `exclude=`, and year filters like
  any search (a `'lexical'` broad search fuses only the text legs).
- **Paginate by repeating the broad knobs** — `page=2` must carry the
  same `queries=`/`answers=`/`per_paper=`, or it silently switches to
  single-query ordering and duplicates page 1. The `Next:` trailer
  echoes the full call — paste it verbatim.
- The headline shows a returned count with no "of K" total — there's
  no honest lexical total for a fused set; the trailer offers
  `page=N+1` while candidates remain.

### good=True — deep search (async campaign)

When even the fused list is too much to read yourself, hand the
judging off: `search(kind='paper', q='…', good=True)` doesn't return
hits — it queues a background *campaign* that runs the broad fusion,
fans the candidate pool out to cheap LLM triage children, and merges
their keep/relevance verdicts into one ranked list:

```python
search(
    kind="paper",
    q="oxygen evolution overpotential on NiFe",
    queries=["NiFe oxyhydroxide OER overpotential"],  # optional seeds
    good=True,
)
# → deep search queued: job=8123 status=queued
#   poll: get(kind='job', id=8123)
```

Poll `get(kind='job', id=8123)` until `STATUS:succeeded` — the merged
verdict lands in the job summary and `meta.result` (`{want, chunks:
[{handle, paper, relevance, why, best_quote}], considered, kept,
children, partial, …}`). Then read the winners via their handles.

- **Reuse:** an identical `q`/`queries`/`answers` re-submit attaches to
  the in-flight campaign instead of duplicating it.
- **Bounds:** a few campaigns run concurrently (default cap 3); over
  the cap you get a BadInput — retry later or poll a running one.
- **When:** a big question you'd otherwise triage across 50+ raw hits.
  For interactive lookups, plain broad search is faster; for an
  identifier, `mode='lexical'`.

## Search the whole corpus
## Find something but I don't know which kind
## Cross-kind search — let the runtime pick

```python
search(q="Z-scheme photocatalysis")  # all kinds
search(kind="*", q="topic:x")  # explicit wildcard
search(kind="paper,patent", q="Z-scheme")  # subset via comma-list
```

When `kind=` is omitted (or `'*'` / `'all'` / `'any'` / `''`), search
fans out across every kind whose handler supports it. Each hit is
tagged with its source kind. Streams merge by rank, so a strong hit in
`memory` can out-rank a weaker hit in `paper`.

## See more results
## Page through search hits beyond the first page
## What if there are more hits than I see?

```python
search(kind="paper", q="photocatalysis", page=2)
search(kind="paper", q="photocatalysis", page=3, page_size=20)
```

`page=1` is the default. Bump `page=` to walk results. `page_size=`
sets the page size (default 10, max 100) — *not* a quality cutoff
despite the name.

Broad-retrieval searches paginate the same way, but the `page=N+1`
call must repeat the same `queries=`/`answers=`/`per_paper=` arguments
(see "Broad retrieval" above) — dropping them switches to the
single-query ordering mid-walk.

`page=`/`page_size=` above is a **search-level** knob — each call is
independent, safe to fire however you like. Don't confuse it with
`more(cursor=...)`, the separate **MCP-transport** mechanism that
continues a single response too large for one frame (a `⚠️ Truncated`
footer on an oversized hit table). A `more()` cursor is single-use,
expires in a few minutes, and lives only in the backend process that
minted it — drain it sequentially, never fire several `more()` calls
in parallel (a batched call gets "no such cursor in this process").
Full mechanics: `precis-toon`.

## Filter search results by tag
## Find refs tagged with topic:X
## Combine search with a tag axis

```python
search(kind="paper", q="photocatalysis", tags=["topic:noxrr"])

search(kind="patent", tags=["cpc:B01J27/24", "country:ep"])

search(kind="todo", tags=["STATUS:open", "PRIO:high"])

search(kind="memory", q="", tags=["pinned"])
```

AND semantics — `tags=['A', 'B']` matches refs carrying *both* tags.
Closed-vocab axes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`) are kind-gated;
open tags (`topic:`, `project:`, `pinned`, ...) are universal. See
`precis-tags` for the axis matrix.

### Enumerating a tag ≠ searching within it — a counting trap

`tags=` alone is a **complete enumeration** by recency: `search(kind='gripe',
tags=['STATUS:open'])` returns *every* open gripe (paginated, "N of K"
header). Add a `q=` and it becomes a **ranked filter**: a ref must *also*
lexically match `q` to appear, so the result is often far fewer. The mode
switch is real and easy to miss.

Use the **no-`q`** form for counting / inventory / "is the backlog clear?"
questions. Reach for `q=` only when you mean "the tagged refs that mention
X". Numeric-ref kinds flag a `q=`-induced drop in the header (`⚠ N of M …
entries tagged […] also match q`) — but that only helps if you read it;
prefer no-`q` when you want the whole set.

```python
search(kind="gripe", tags=["STATUS:open"])  # ✅ how many are open (all of them)
search(
    kind="gripe", q="timeout", tags=["STATUS:open"]
)  # open gripes mentioning "timeout" (a subset!)
```

Some kinds fold a **default status** into this: `gripe` defaults to
`STATUS:open` and `finding` to `STATUS:established` when you pass no
`status=`/`STATUS:` tag. Pass `status='*'` to opt out. The default is named
in the response header, never silent.

## Search inside a specific paper or ref
## Where does this paper mention X?
## Scope a search to one ref's contents

```python
search(kind="paper", q="Z-scheme", scope="pa5")  # handle from get/search output
search(kind="patent", q="heterojunction", scope="ep4123456a1")
```

`scope=` restricts to one ref's blocks. Useful for "where in this
paper does X come up?"

## Drop specific refs from results
## Hand-skip known-irrelevant papers
## Search but ignore these refs

```python
search(kind="paper", q="photocatalysis", exclude=["pa5", "pa12"])  # handles from output
```

Ref-level — a handle (`pa<id>`), slug, chunk selector, or DOI all resolve to
the underlying ref; unknown entries are silently ignored. `exclude=` is the
skip-list for known-irrelevant refs, not a paging mechanism — use `page=`
for that.

## Best chunks, one per paper, about X, excluding what a draft already cites
## Skip papers a draft (or one section of it) already cites

`exclude=` also accepts **containers** in the same list: a whole draft
(`dr…`) or a draft-chunk subtree (`dc…`) — resolved server-side to every
paper cited anywhere within (`[pa…]` direct, `[pc…]` via its owning
paper, `[fi…]` via a claim hub's grounding/supporter papers). You never
walk the draft's cites by hand:

```python
search(kind="paper", q="wang tile guided self assembly", per_paper=1,
       exclude=["dr42995"])                    # the whole draft's cite closure
search(kind="paper", q="wang tile guided self assembly", per_paper=1,
       exclude=["dc48213"])                     # just that section's closure
```

A `dr…`/`dc…` entry that doesn't resolve raises `BadInput` naming it
(unlike a stale bare slug, which is silently dropped — you named this
container explicitly). See `precis-stubs-help` for the twin external-
discovery form (`get(kind='semanticscholar', exclude=[…])`), which also
flags every hit `held:`/`stub:`/`NEW` against the corpus.

## Find sources a draft hasn't cited yet — query-driven discovery
## uncited= — search for sources you missed, by keyword
## The query-driven twin of view='backfill'

```python
search(kind="paper", q="wang tile guided self assembly", uncited="dr173020")
```

`uncited=<draft>` (a `dr<id>` handle, slug, or bare ref_id) drops every
source that draft already cites — the same closure `exclude=['dr…']`
computes (direct cites, plus a cited claim hub's evidence-**supporters**;
a paper that **contradicts** a cited claim keeps surfacing). The response
names how many refs it excluded:

```
_(uncited=dr173020: 4 already-cited sources excluded)_
```

This is the **query-driven** complement to `get(kind='draft', id=<scope>,
view='backfill')` (`precis-draft-help`): `view='backfill'` programs its own
recall from the draft's own section text; `uncited=` answers a question
*you* phrase. Reach for `view='backfill'` to sweep a whole section for
gaps, `uncited=` when you already have a specific angle.

An unresolvable handle, or one that resolves to a non-draft ref, raises
`BadInput` — `uncited=` never degrades to a no-op filter. Naming a kind
whose search has no exclude-by-ref_id wiring yet (`patent`, `edgar`)
explicitly raises `Unsupported`; the default unscoped fan-out instead
drops it from the merge and says so (`_(uncited=: skipped edgar — …)_`).

## What does the ⚠ on a paper hit mean?
## Why did a retracted paper rank so low?

A `⚠ RETRACTED` / `⚠ corrected` / `⚠ expression of concern` prefix on a
row means `refs.retraction_status` is set on that paper. Retracted hits are
downranked hard, the softer notices mildly — but **never excluded**: a
retracted paper is often exactly what you were looking for.

No `⚠` means "no notice on file" — usually **"nobody has checked"** rather
than "clean"; checks are demand-driven. Don't read a bare row as an
integrity clearance. To actually check, use `get(kind="provenance",
q="<doi>")` → `precis-provenance-help`.

## What is the `Title match —` line above the table?
## I searched a paper's exact title and the rows look unrelated

Pasting a whole title into `q=` is a weak lexical query: full-text search
strips `attention is all you need` to its stemmed content words, so any
content-dense body that repeats those words outranks the paper's own
short card. Paper search compensates — on page 1 with no
`scope=`/`tags=`/`after=`/`before=`, a near-exact trigram match on
`refs.title` is promoted to the top of the table **and** named above it:

```
Title match — the paper record:
  pa2928 [held] — Ashish Vaswani et al. (2017). Attention is All you Need
```

Read that line, not the row it promoted — the promoted row is still a
*chunk* (often the paper's boilerplate first chunk), so its keywords can
look nothing like your query. `held` vs `want` says whether the PDF is in
the corpus. For every title match as a record, use `search(kind='paper',
title='…')`.

## Find the right skill for a task
## Which skill explains how to do X?
## Discover a skill by topic

```python
search(kind="skill", q="how do I edit a markdown file")
search(kind="skill", q="paginate paper search")
search(kind="skill", q="patent prior art")
```

Natural-language queries work — phrase your query the way you'd ask
it. Skill hits whose subject kind isn't loaded in the current build
are prefixed `[unwired]` so you don't follow them to no-op verbs.
This is the standard first move on any non-trivial task.

## Find patents not yet in the local store
## Search EPO directly via OPS
## How do I find a patent that isn't ingested yet?

```python
search(kind="patent", q="photocatalysis", reach="remote")
search(kind="patent", tags=["cpc:B01J27/24"], reach="remote")
```

`reach=` is patent/EDGAR-only. `'both'` (default) merges local + remote;
`'local'` skips OPS/SEC; `'remote'` returns only hits *not* already in
the local store. CQL details in `precis-patent-search-help`.

## See also

```python
get(kind="skill", id="precis-overview")  # verbs and kinds
get(kind="skill", id="precis-paper-help")  # paper-specific search shape
get(kind="skill", id="precis-patent-search-help")  # CQL + reach= matrix
get(kind="skill", id="precis-tags")  # axis vocabulary
get(kind="skill", id="precis-relations")  # link vocabulary
get(kind="skill", id="precis-toc-help")  # drilling into hits via /toc
```
