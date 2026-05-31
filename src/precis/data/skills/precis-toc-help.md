---
id: precis-toc-help
title: precis — TOC machinery (smart segmentation + cross-kind address grammar)
status: active
tier: 1
floor: any
applies-to: get(view='toc'), slug~N / slug~A..B / slug/toc paths
last-updated: 2026-05-31
---

# precis-toc-help — table-of-contents machinery

How the `view='toc'` shape works on every TOC-capable kind, what
address forms resolve where, and how the embedding-cluster smart-TOC
falls back when explicit section structure is missing.

## The three ways to ask for a TOC

```python
get(kind='paper', id='cai23', view='toc')        # kwarg form
get(kind='paper', id='cai23/toc')                # path form
get(kind='skill', id='precis-overview/toc')      # same on skills
get(kind='paper', id='cai23~63..89', view='toc') # sub-range, recursive
get(kind='paper', id='cai23~63..89/toc')         # sub-range, path form
```

Path form (`slug/toc`) and kwarg form (`view='toc'`) are
interchangeable — pick whichever reads better. Sub-range
addressing (`slug~A..B`) composes with both.

## Output shape

A **TOON table** (one row per segment) with **matryoshka-ordered
keywords** (most-distinctive first) and, for papers, an indented
**query-aligned excerpt sub-line** drawn from
`ref_segment_sentences`.

Two column shapes depending on whether the renderer is in
**H2-driven mode** or **embedding-clustered mode**:

```
# slug TOC — K segments via embedding clustering

{handle	keywords}
slug~0..14	kw1, kw2, kw3, kw4, kw5
  - excerpt @ ~3: "Top central sentence of the segment, query-aligned."
slug~15..38	kw1, kw2, kw3, kw4, kw5
  - excerpt @ ~22: "Another segment's representative sentence."
…
```

```
# slug TOC — K segments via H2 sections

{handle	heading	keywords}
slug~0..7	Introduction
  - excerpt @ ~2: "..."
slug~8..20	Methods
  - excerpt @ ~12: "..."
slug~21..40	Results
…
```

The `keywords` column is empty when the H2 heading is informative
enough on its own. Excerpt sub-lines are omitted silently when a
segment has no stored sentences (worker hasn't run yet, or all
sentences failed compute).

## Keyword ordering — matryoshka by distinctiveness

Per-segment keywords are ordered **most-distinctive first**, not
most-frequent: each candidate's score is
`cos(phrase, segment_centroid) - λ·max(cos(phrase, sibling_centroids))`
with λ ≈ 0.3. So `keywords[0]` reads as "what's unique about this
segment vs. the rest of the paper," not "what does this paper
overall talk about." Truncating to the top-3 still leaves you with
the most-segment-specific picks (truncate-friendly = matryoshka).

Stored keyword records carry `{long, short, aliases[], score}` —
the display column prefers `short` (when there's a Schwartz-Hearst
expansion in the per-paper legend) and falls back to `long`. The
denormalized `forms TEXT[]` array on `ref_segments` (long + short +
aliases for every keyword) is GIN-indexed so cross-paper queries
by any surface form hit the index directly:

```sql
SELECT ref_id FROM ref_segments
 WHERE forms @> ARRAY['MOF'];   -- matches "MOF", "MOFs", "Metal-Organic Framework", …
```

## When H2 mode fires vs embedding mode

H2 mode requires:
* At least 3 H1/H2 headings detected in the body, AND
* H2-named ranges cover at least 80 % of the chunks.

Otherwise the renderer falls back to **embedding-cluster mode**:
**DP-uniform-cost segmentation** on the bge-m3 chunk vectors —
optimal K-segmentation minimizing within-segment cosine dispersion.
K is `ceil(body_chunks / 20)` clamped to `[3, 9]` and further
clamped to the body chunk count. The algorithm is deterministic
(same input → same boundaries) and cached per ref.

For papers without explicit sectioning (preprints, single-column
LaTeX exports, anything that's body-text-only) embedding mode is
the only useful TOC.

## Recursive sub-segmentation

Any `slug~A..B` range can itself be `view='toc'`'d. The renderer
re-runs the algorithm on that sub-range — finds 3–9 sub-segments
within it — and emits the same TOON shape with the absolute chunk
positions in the handles.

```python
get(kind='paper', id='cai23~63..89', view='toc')
# → sub-segments cai23~63..70, ~71..78, ~79..84, ~85..89
```

Recursion bottoms out when the range has fewer than `K_MIN`
chunks — at that point each chunk becomes its own row, no
further segmentation. Practical recursion depth: 2-3 levels
before you're at single-chunk granularity.

## Persistence + caching

For paper kind the discovery layer is **pre-computed at ingest** by
`precis.workers.segment_toc.build_segments` and persisted to
`ref_segments` + `ref_segment_sentences`. The TOC renderer
(`precis.utils.toc_db.render_from_store`) reads those rows directly
— no per-request DP + KeyBERT recompute.

Versioning is lazy: every row carries `segmentation_version`,
`extractor_version`, `embedder_name`, and (on sentences)
`sentence_splitter_version`. A mismatch on read means "treat as
cache-miss, recompute, overwrite." So pipeline upgrades self-heal
without manual sweeps.

Worker idempotency: re-running `build_segments(ref_id=N, ...)`
does `DELETE FROM ref_segments WHERE ref_id = N` then re-INSERTs.
Sentences cascade-delete via the FK. So `precis worker --only
segments` is safe to drive repeatedly.

For non-paper kinds without persistent segments yet (skills,
decisions), `precis.utils.toc.render` is still the in-memory path
and uses an LRU keyed on `(ref_id, kind, chunker_version,
embedder_name, SEGMENTATION_VERSION, scope)`.

## Cross-kind support

Today: `paper` and `skill`. Adding TOC support to another kind
means implementing one method on its handler:

```python
def chunks_for_toc(self, ref) -> ChunksForToc:
    return ChunksForToc(
        chunks_text=...,
        embeddings=...,    # None when the kind has no per-chunk vectors
        h2_boundaries=..., # () when the kind has no section markers
        positions=...,     # canonical N for ``slug~N`` handles
    )
```

For paper kind specifically, the discovery layer is **pre-computed
at ingest** by `precis.workers.segment_toc.build_segments` and
persisted to `ref_segments` + `ref_segment_sentences`. The TOC
renderer (`precis.utils.toc_db.render_from_store`) reads those rows
directly — no per-request DP + KeyBERT recompute. Sub-line excerpts
are the top central sentence per segment. For kinds without
persistent segments yet (skills, decisions), the in-memory
`precis.utils.toc.render` is still the path.

## See also

- `precis-paper-help` — paper-specific TOC + drill-in examples
- `precis-search-help` — cluster-context hints in search trailers
- `precis-overview` — uniform address grammar (`slug~N`, `/toc`)
- `precis-toon` — wire format the TOC table uses
