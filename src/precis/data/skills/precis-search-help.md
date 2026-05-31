---
id: precis-search-help
title: precis — the search verb (mechanics + cross-kind fan-out)
status: active
tier: 1
floor: any
applies-to: search (every kind that supports it)
last-updated: 2026-05-24
---

# precis-search-help — search across kinds

`search` is the discovery verb. Two streams under the hood — lexical
(tsvector / substring) and semantic (bge-m3 cosine) — RRF-fused into
one ranked list. The same shape works for one kind, several kinds,
or the cross-kind fan-out.

```python
search(q='your query')                          # cross-kind fan-out
search(kind='paper', q='photocatalysis')        # one kind
search(kind='paper,patent', q='NOxRR')          # comma-list
search(kind='*', q='topic-x')                   # explicit '*' (also: 'all', 'any')
```

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `q` | str | required | Free-text query. Lexical + semantic, hybrid-fused. |
| `kind` | str | None → `*` | One kind, comma-list, or `*` / `all` / `any` / `''` for fan-out. |
| `scope` | str | None | Restrict to one ref's blocks (slug or numeric id). |
| `top_k` | int | 10 | Max results. **Must be a positive int ≤ 100** — wire-level cap. |
| `tags` | list[str] | None | Per-kind tag filters. Closed-vocab axes (`cpc:B01J27/24`) and open tags (`topic-x`) both flow. |
| `source` | str | None | **Patent only**: `'both'` / `'local'` / `'remote'`. Ignored elsewhere. See `precis-patent-search-help`. |
| `exclude` | list[str] | None | Ref slugs to omit. Use to paginate. |

## `top_k` is capped at 100

Larger values are rejected to bound response size and protect smaller
models' context windows. To see more, paginate via `exclude=`.

## Result shape — TOON

Search responses render hits as a **TOON table** so the agent parses
one shape across every kind. For papers, each hit row is paired
with an **indented excerpt sub-line** drawn from the persistent
discovery layer (`ref_segment_sentences`):

```
{handle	chunk_keywords}
cai23~91	secondary electron image outlines, ToF-SIMS characterization, …
  - excerpt @ ~91: "ToF-SIMS depth profiling confirms a uniform F-rich layer at 30 nm."
cai23~45	cross-sectional SEM images, PEO membrane doped, …
  - excerpt @ ~45: "PEO membranes doped with LiBF4 show ionic conductivity above 10⁻⁴ S cm⁻¹."
```

* `handle` is a copy-pasteable `id=` for `get` — drilling into any
  hit is a one-call follow-up (`get(kind='paper', id='cai23~91')`).
* `chunk_keywords` is RAKE-extracted from the matched block (with
  per-paper abbreviation substitution — `FTIR` not "Fourier
  Transform Infrared Spectroscopy").
* `- excerpt @ ~N: "..."` is the **query-aligned central sentence**
  from the segment that contains the hit chunk. The sentence is
  reranked against your query embedding via pgvector cosine, so the
  sub-line shows the segment's most-on-topic sentence — not just
  the most central one. The `~N` annotation tells you which chunk
  the excerpt came from (often the same as the hit chunk, sometimes
  a nearby one in the same segment).

**Excerpts are triage, not citations.** They help you decide whether
to drill in — *not* whether to cite. Always fetch the verbatim
chunk and run the verifier before persisting a citation. See
`precis-citation-help` for the write-side workflow.

**Skill search** uses `{slug, section, keywords}` instead — slug
prefixed with `[unwired]` when the skill documents a kind that isn't
loaded in this build.

## Cluster context in the trailer

For paper search, the `Next:` block includes a **cluster hint** for
the top hit — pointing at the segment of the paper that contains
the matched chunk:

```
Next:
  get(kind='paper', id='cai23~91')                    - read the full text of any hit
  get(kind='paper', id='cai23~41..93', view='toc')    - sub-TOC of the segment containing the top hit
  search(kind='paper', q='tof', exclude=[…])          - next page
  search(kind='paper', q='tof', scope='cai23')        - narrow to this paper
```

The cluster handle comes from the same embedding-cluster TOC the
paper handler emits for `view='toc'`. So `~41..93` says "the top
hit lives in a 53-chunk segment about XPS / surface chemistry — drill
into that range for context."

## Cross-kind fan-out

When `kind` is omitted, `'*'`, `'all'`, `'any'`, `''`, or a comma-list,
search fans out across every kind whose handler declares
`supports_search_hits=True`. Each hit is tagged with its source kind in
the response. RRF-merged, so per-kind relevance signals combine.

```python
search(q='Z-scheme photocatalysis')             # all kinds
search(kind='paper,patent', q='Z-scheme')       # subset
```

Comma-lists narrow the fan-out without forcing a single-kind shape.

## Pagination via `exclude=`

`exclude=` drops ref slugs from the result. Use to paginate:

```python
# Page 1.
search(kind='paper', q='photocatalysis', top_k=5)
# → 5 of 47 ...
#   ## 1. wang2020state~12 ...
#   ## 2. kim2024electro~7 ...
#   ...

# Page 2 — pass back the slugs from page 1.
search(kind='paper', q='photocatalysis', top_k=5,
       exclude=['wang2020state', 'kim2024electro', 'liu2022zscheme',
                'park2023nitrate', 'choi2021hybrid'])
# → 5 of 42 ...
```

Notes:

- **Coarse / ref-level.** `exclude=['wang2020state']` drops every
  block of that paper. Selectors and view paths are stripped — a
  hit handle (`'wang2020~12'`) and a DOI (`'10.1111/x'`) both
  resolve to the bare slug.
- **`LIMIT` applies after exclusion.** `top_k=5` with five excluded
  refs really does return five new hits, not zero. The `N of K`
  header reports the remaining universe.
- **Stale slugs are silent no-ops.** Unknown / soft-deleted slugs
  don't fail the call.
- **Continuation pre-filled.** When `total > len(hits)`, the
  response trailer is a copy-pasteable
  `search(... exclude=[...])` that already merges prior exclude
  with this page's slugs.
- Currently honoured by `kind='paper'`; other block-level kinds
  ignore it.

## Tag filtering

`tags=` accepts a list of tag strings. Two flavours:

- **Closed-vocab axes** like `cpc:B01J27/24`, `topic-noxrr`,
  `STATUS:done`. Validated per-kind via the axis matrix. See
  `precis-tags` and `precis-paper-tag-axes`.
- **Open tags** like `pinned`, `2026-q2`, `fbproj`. Free-form;
  no validation beyond charset.

```python
search(kind='paper', q='photocatalysis', tags=['topic-noxrr'])

search(kind='patent', tags=['cpc:B01J27/24', 'country:ep'])

search(kind='memory', tags=['topic-noxrr', 'pinned'])

search(kind='todo', tags=['STATUS:open', 'PRIO:hi'])
```

## Scope to one ref

`scope=<slug-or-id>` restricts search to the blocks of one ref:

```python
search(kind='paper', q='Z-scheme', scope='wang2020state')
search(kind='patent', q='heterojunction', scope='ep4123456a1')
```

Useful for "where in this paper does X come up?".

## Skill discovery (the meta-use)

`kind='skill'` is the discovery surface. The skill index is
embedding-backed (bge-m3 cosine over H2-chunked help docs) so
natural-language queries land:

```python
search(kind='skill', q='how do I edit a markdown file')
search(kind='skill', q='paginate paper search')
search(kind='skill', q='patent prior art')
```

This is the standard first action on any non-trivial task — the
ranked list of skills is faster than reading the full TOC.

## Per-kind notes

- **paper** — block-level hybrid; `exclude=` pagination supported;
  `tags=` includes the topic axis. See `precis-paper-help`.
- **patent** — extra `source=` knob; OPS integration; prior-art
  sweep mode. Full matrix in `precis-patent-search-help`.
- **memory / gripe / conv / fc / quest / todo** — block-level
  hybrid over the ref's text + summary.
- **markdown / plaintext / tex** — block-level hybrid; respects
  the `workspace` tag for sandbox refs.
- **python** — symbol-level lexical + semantic; uses the in-memory
  AST index.
- **calc / math / web / wolfram / youtube** — read the appropriate
  per-kind skill via `search(kind='skill', q='<kind>')`.

## See also

- `precis-overview` — kind topology and addressing conventions
- `precis-patent-search-help` — the `source=` matrix in detail
- `precis-paper-tag-axes` — paper tag vocabulary
- `precis-tags` — cross-kind tag conventions
- `precis-toc-help` — cluster context (the trailer's "sub-TOC of the segment containing the top hit" hint)
