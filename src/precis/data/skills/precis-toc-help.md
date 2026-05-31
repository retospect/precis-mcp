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

A **TOON table** (one row per segment), optionally followed by an
abbreviation legend and a shared-phrases footer.

Two column shapes depending on whether the renderer is in
**H2-driven mode** or **embedding-clustered mode**:

```
# slug — TOC (N chunks, K segments via embedding clustering)
{handle    keywords}
slug~0..14    keyword1, keyword2, keyword3, keyword4, keyword5
slug~15..38   keyword1, keyword2, keyword3, keyword4, keyword5
…
```

```
# slug — TOC (N chunks, K H2 sections)
{handle  heading    keywords}
slug~0..7   Introduction
slug~8..20  Methods
slug~21..40 Results
…
```

The `keywords` column is empty when the H2 heading is informative
enough on its own; the heuristic only fills it in for "stupid"
headings (`Methods`, `Results`, `4.2`, empty, numeric labels) that
don't disambiguate content.

## Trailers

* **Abbrevs legend** (`Abbrevs: FTIR (Fourier Transform Infrared), …`)
  — Schwartz-Hearst-detected abbreviations applied to the source
  text before RAKE. Substituting "FTIR" for "Fourier Transform
  Infrared" keeps per-row keywords short and uses the canonical
  short form a domain expert would write.
* **Shared phrases** (`Shared across segments: …`) — phrases that
  appear in ≥ 60 % of segments. Hidden from per-row keywords so
  each row's keywords disambiguate from the others. Often the
  paper's central topic — `lithium-mediated nitrogen reduction`
  for a battery paper, `Z-scheme photocatalysis` for a catalysis
  paper.

## When H2 mode fires vs embedding mode

H2 mode requires:
* At least 3 H1/H2 headings detected in the body, AND
* H2-named ranges cover at least 80 % of the chunks.

Otherwise the renderer falls back to **embedding-cluster mode**:
TextTiling-style sequential segmentation on the bge-m3 chunk
vectors. Adjacent-chunk cosine drops mark topic shifts; depth +
knee-point selection picks K segments bounded to `[3, 9]`. The
algorithm is deterministic — same input → same boundaries — and
cached per ref.

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

## Caching

In-memory LRU keyed on `(ref_id, kind, chunker_version,
embedder_name, SEGMENTATION_VERSION, scope)`. Capacity 256
entries. First TOC view of a paper computes embeddings cosine +
RAKE keywords; subsequent calls (same paper, same scope) return
in microseconds.

The same cache powers the **cluster hint** in paper search
trailers — after the first TOC view of a paper, finding which
segment contains any given hit chunk is a single dictionary
lookup. See `precis-search-help § Cluster context`.

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
