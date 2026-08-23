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
applies-to: search (every kind that supports it)
status: active
---

# precis-search-help — search across kinds

Hybrid lexical + semantic search. Returns ranked handles (`pc<chunk_id>`,
e.g. `pc40`) you paste straight into `get(id=…)` to drill in — the handle's
prefix infers the kind. Order is the relevance signal — there is no honest
numeric score.

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
search(kind="patent", q="X", source="remote")  # patent-only knob
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

## Ranking mode — hybrid (default), lexical, semantic, or verbatim

By default `search` is **hybrid**: it fuses a lexical pass (Postgres
full-text) with a semantic pass (embeddings) by reciprocal-rank fusion.
That's the right default for "find me things about X". But you can pin
the ranking with `mode=`:

| `mode` | What it does | Reach for it when |
|---|---|---|
| `'hybrid'` *(default)* | RRF of lexical + semantic. | General recall — concepts *and* keywords. |
| `'lexical'` | Postgres FTS only; no embedding. | You know the **exact string** — an identifier, acronym, surname, code token, a numeric like `1.523 eV`, or an exact phrase. Embeddings blur these; lexical is precise and deterministic. Also the honest tool when the embedder is down (hybrid silently degrades to this anyway). |
| `'semantic'` | Embedding cosine only. | Pure conceptual / paraphrase recall where the wording won't match but the meaning does, and keyword noise is hurting precision. Degrades to lexical if the embedder is unavailable. |
| `'verbatim'` | Chunks whose per-chunk **KeyBERT keywords** contain **all** your query words (GIN `@>` containment; embedder-independent). No relevance gradient — newest chunk first. | You want only chunks a topic model actually tagged with your term(s) — a topical filter tighter than FTS. Each query word must appear as a *distinct* keyword, so it's terms, not phrases (`'oxygen evolution'` = both words present, not the 2-gram). Empty query returns nothing. |

```python
search(kind="paper", q="MoS2 monolayer", mode="lexical")  # exact term recall
search(q="ways to stop catalyst poisoning", mode="semantic")  # paraphrase recall
search(
    kind="paper", q="perovskite stability", mode="verbatim"
)  # chunks keyworded BOTH terms
```

`mode=` works on a single kind **and** across the cross-kind fan-out.
Scores are never comparable *across* modes (RRF score vs. cosine
distance vs. lexical rank) — within a result list, more-relevant is
always first.

## Notation — the corpus stores one spelling per quantity

Claim sentences (`kind='finding'`) are normalized to a **UTF-8 notation
canon**: `kΩ` not `kOhm`, `µ` not `u`/`mu`, `Å`, `±`, spaced `63 °C` but tight
`85°`. Full rules: **`precis-notation-canon`**.

This matters to you as a searcher because the lexical leg matches *tokens*, not
meanings — `kOhm` and `kΩ` are simply different strings. Search compensates: an
ASCII query is auto-canonicalized and run as an **extra** leg, so `40 kOhm`
finds a claim written `40 kΩ`. You don't have to type the symbols.

Two limits worth knowing:

- The compensation runs **ASCII → canon**, the direction agents actually type.
  The reverse (a `kΩ` query against a row not yet normalized) leans on the
  semantic leg — so prefer ASCII in queries, or `mode='semantic'`.
- Letter sub/superscripts stay ASCII by canon (`E_g`, `E_F`, `K_d`, `2^N`).
  Search for `E_F`, not `E_F` with a subscript glyph.

When you need an exact quantity, `mode='lexical'` plus the canonical spelling
is the precise tool; embeddings blur numerals.

There is no `mode='regex'` here — that only exists on `kind='draft'`
(`precis-draft-help`, "Search a draft") and errors with `unknown search
mode 'regex'` on any other scope. For a literal/pattern hunt on other
kinds, use `mode='lexical'` (exact string/keyword match) or
`mode='verbatim'` (chunk tagged with all query words).

| Arg | Type | Meaning |
|---|---|---|
| `q` | str | Free-text query. |
| `mode` | str | Ranking strategy: `'hybrid'` (default) / `'lexical'` / `'semantic'`. See below. |
| `kind` | str | One kind, comma-list, or `'*'` / `'all'` / `'any'` / `''` for fan-out. |
| `page` | int | Page number (default 1). |
| `page_size` | int | **Page size** (default 10, max 100). Not a match-quality cutoff despite the name. |
| `tags` | list[str] | Per-kind tag filters; AND semantics. |
| `scope` | str | Restrict to one ref's blocks. |
| `exclude` | list[str] | Skip-list (specific slugs to drop). `page=` is the normal pagination. |
| `source` | str | Patent only: `'both'` (default) / `'local'` / `'remote'`. |
| `view` | str | Alternate result shape. `view='dreamable'` returns a salience-focus-region pick from the most-due seed (cross-kind only; `q=` not required for this view). `view='stubs'` returns the paper-acquisition backlog — paper refs with an external id but no PDF yet (`q=` ignored; see `precis-stubs-help`). `view='chase-queue'` is a tighter, DOI-only, never-tried-first slice of the same backlog (`q=` ignored). |
| `angle` | float | Salience-rotation search; pairs with `like=` (or `q=` for a seed). See `precis-dreaming-help`. |
| `like` | str | Seed ref handle for `angle=` search; e.g. `like='pc40'`. |
| `status` | str | Shorthand for `tags=['STATUS:<value>']` on kinds with a STATUS axis. `finding` defaults to `'established'`, `gripe` to `'open'`; pass another value for a specific cohort, or `'*'` for all regardless. On kinds with no STATUS default it simply adds the filter when given, and is ignored when omitted. |
| `queries` | list[str] | **Broad retrieval** (paper): extra question rephrasings, each fused as its own ranked leg. Up to 8. See below. |
| `answers` | list[str] | **Broad retrieval** (paper): hypothetical answer passages (HyDE) — short paragraphs you'd *expect* a relevant chunk to read like; embedded and fused. Up to 8. See below. |
| `per_paper` | int | **Broad retrieval** (paper): cap hits per paper to spread results across more sources (breadth triage). |
| `title` | str | **Byline lookup** (paper): find a paper by its title. Returns paper *records* (handle + citation + cite path), not block hits. Matches `refs.title` via trigram + FTS, held copies first. See `precis-paper-help`. |
| `author` | str | **Byline lookup** (paper): find papers by an author name (surname or full). Same record-row shape as `title=`; matches the structured `refs.authors` byline. Pass one of `title=`/`author=`, not both. |
| `folder` | int/str | **Placement scope** (ADR 0045): restrict hits to one folder's live subtree. Accepts the id, `'folder:N'`, the `fo<N>` handle, or the folder's unique name. Forces the cross-kind fan-out even with a single `kind=`; works with `tags=`-only sweeps too. See `precis-folder-help`. |

## Broad retrieval — when the gold hides behind the wording
## Find more, better, more-diverse chunks for one question
## High-recall paper search (multi-query + HyDE)

A single phrasing is fragile: the best chunk often loses just because it
words the idea differently than you did. Instead of firing 5 separate
searches and eyeballing each, hand `search` **several angles at once** and
let it fuse them. A chunk that surfaces across phrasings rises to the top.

Two knobs, both paper-side, both fused with `q` by reciprocal-rank fusion:

- `queries=[…]` — **rephrasings of the question** (synonyms, broader /
  narrower framings, the sub-questions hiding inside it). Up to 8.
- `answers=[…]` — **hypothetical answer passages** (HyDE): write 1–3
  short paragraphs the way you'd expect an *ideal source chunk* to read,
  and let their embeddings pull in real chunks that look like them. This
  is often the single biggest lever for technical queries — the fake
  answer lives in "chunk space", not "question space". Up to 8.

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

Then **poke around** before you trust a hit: paste any returned handle
into `get(id='pc…')` to read it in full, and `search(kind='paper',
scope='pa…', q='…')` to read more of that paper around the chunk. Cite
or write a memory once you've confirmed the context — don't cite off the
keyword row alone.

Rules of thumb:
- Reach for this on **research / triage** questions ("what does the
  corpus say about X?"), not exact-string lookups — for an identifier or
  acronym use `mode='lexical'`.
- 3–5 `queries` + 1–2 `answers` is plenty; more legs ≠ better.
- `per_paper=2` is a good default when you want *breadth* (many papers);
  drop it when you want to mine one paper deeply.
- Honors `mode=` (a `'lexical'` broad search fuses only the text legs),
  `tags=`, `scope=`, `exclude=`, and the year filters like any search.
- **Paginate by repeating the broad knobs**: `page=2` must carry the
  *same* `queries=`/`answers=`/`per_paper=` — the fused ordering only
  exists when every page fuses the same legs. A bare
  `search(q=…, page=2)` runs the single-query path: a different
  ordering, so you'd see duplicates of page 1 and miss fused hits. The
  `Next:` trailer echoes the full call for you — paste it verbatim.
- A broad headline shows the returned count without an "of K" total —
  there is no meaningful lexical total for a fused set. The trailer
  offers `page=N+1` whenever more fused candidates remain.

### good=True — deep search (async campaign)

When even the fused list is too much to read yourself, hand the
judging off: `search(kind='paper', q='…', good=True)` does **not**
return hits. It queues a background *campaign* that runs the broad
fusion, fans the candidate pool out to cheap LLM triage children, and
merges their keep/relevance verdicts into one ranked list. You get an
async handle back immediately:

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
verdict lands in the job summary (human-readable curated list) and
`meta.result` (`{want, chunks: [{handle, paper, relevance, why,
best_quote}], considered, kept, children, partial, …}`). Then read the
winners via their handles as usual.

- **Reuse:** an identical `q`/`queries`/`answers` re-submit attaches to
  the in-flight campaign instead of starting a duplicate (the ack says
  so).
- **Bounds:** a few campaigns may run concurrently (default cap 3);
  over the cap you get a BadInput — retry later or poll a running one.
- **When to use it:** slow-but-clever triage of a big question where
  you'd otherwise read 50+ raw hits. For interactive lookups, plain
  broad search (`queries=`/`answers=`, no `good=`) is faster; for an
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
tagged with its source kind. Streams merge by rank, so a strong hit
in `memory` can out-rank a weaker hit in `paper`.

## See more results
## Page through search hits beyond the first page
## What if there are more hits than I see?

```python
search(kind="paper", q="photocatalysis", page=2)
search(kind="paper", q="photocatalysis", page=3, page_size=20)
```

`page=1` is the default. Bump `page=` to walk results. `page_size=` sets
the page size (default 10, max 100) — *not* a quality cutoff despite
the name.

Broad-retrieval searches paginate the same way, but the `page=N+1` call
must repeat the same `queries=`/`answers=`/`per_paper=` arguments (see
"Broad / high-recall retrieval" above) — dropping them switches to the
single-query ordering mid-walk.

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
tags=['STATUS:open'])` returns *every* open gripe (paginated, with an "N of
K" header). Add a `q=` and it becomes a **ranked filter**: a ref must *also*
lexically match `q` to appear, so the result is "the tagged refs that also
match q", capped by relevance — often far fewer. The mode switch is real and
easy to miss.

So for **counting / inventory / "is the backlog clear?"** questions, use the
**no-`q`** form. Reach for `q=` only when you actually mean "the tagged refs
that mention X". When a `q=` drops matches from a tagged set, numeric-ref
kinds flag it in the header (`⚠ N of M … entries tagged […] also match q`)
so the truncation is visible rather than silent — but the header only helps
if you read it; prefer the no-`q` form when you want the whole set.

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
the underlying ref; unknown entries are silently ignored. `exclude=` is the skip-list for
known-irrelevant refs, not a paging mechanism — use `page=` for that.

## What does the ⚠ on a paper hit mean?
## Why did a retracted paper rank so low?

A `⚠ RETRACTED` / `⚠ corrected` / `⚠ expression of concern` prefix on a
row means `refs.retraction_status` is set on that paper. Retracted hits are
downranked hard, the softer notices mildly — but **never excluded**: a
retracted paper is often exactly what you were looking for, and silently
dropping it would be indistinguishable from a broken index.

No `⚠` means "no notice on file", which is usually **"nobody has checked"**
rather than "clean" — checks are demand-driven, so coverage is sparse by
design. Don't read a bare row as an integrity clearance. To actually check,
use `get(kind="provenance", q="<doi>")` → `precis-provenance-help`.

## What is the `Title match —` line above the table?
## I searched a paper's exact title and the rows look unrelated

Pasting a whole title into `q=` is a weak lexical query: Postgres FTS
strips `attention is all you need` to `'attent' & 'need'`, so any
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
the corpus. For the full list of title matches as records, use
`search(kind='paper', title='…')`.

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
search(kind="patent", q="photocatalysis", source="remote")
search(kind="patent", tags=["cpc:B01J27/24"], source="remote")
```

`source=` is patent-only. `'both'` (default) merges local + remote;
`'local'` skips OPS; `'remote'` returns only patents *not* already
in the local store. CQL details in `precis-patent-search-help`.

## See also

```python
get(kind="skill", id="precis-overview")  # verbs and kinds
get(kind="skill", id="precis-paper-help")  # paper-specific search shape
get(kind="skill", id="precis-patent-search-help")  # CQL + source= matrix
get(kind="skill", id="precis-tags")  # axis vocabulary
get(kind="skill", id="precis-relations")  # link vocabulary
get(kind="skill", id="precis-toc-help")  # drilling into hits via /toc
```
