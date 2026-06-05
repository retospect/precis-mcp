# Discovery layer — policy

When to read from `ref_segments` / `ref_segment_sentences`, when to
recompute, what the worker can assume, and where the lazy-
invalidation discipline applies. Companion to
[ADR 0018](../decisions/0018-persistent-discovery-layer.md) and
[`docs/design/storage-v2.md § Discovery layer`](../design/storage-v2.md).

## Read-side policy

**Render from the store, always.** Paper TOC view, search-result
excerpt sub-lines, and the `segment_containing_chunk` cluster-context
hint all go through `precis.utils.toc_db.render_from_store` and the
`SegmentsMixin` accessors. There is no fallback to on-demand
`render_for_ref` for paper kind — when the worker has not populated
rows yet, the renderer returns the "segments not yet computed"
placeholder. Recomputing on-demand would defeat the cache-hit story
and create drift between cold-render and worker-render outputs.

**Skill kind (and any future TOC-capable file kind) still uses the
in-memory `precis.utils.toc.render` path** until those kinds are
ingested as DB refs (storage-v2 step B12). Coexistence is by *kind*,
not for the same ref — no drift risk.

## Write-side policy (worker invocation)

Drive the worker via `precis worker --only segments`. Defaults are
fine: `--batch-size 32`, `--idle-seconds 2`. The worker:

- Claims at most `batch-size` refs that have body chunks
  (`chunks.ord >= 0 AND chunk_kind <> 'references'`) but no
  `ref_segments` rows yet.
- For each: builds the per-handler adapter (`build_paper_adapter`
  for paper kind), runs `build_segments`, commits per ref.
- `--once` makes it process one batch and exit; without it the
  loop sleeps `idle-seconds` between empty passes.

Re-running on the same ref does `DELETE FROM ref_segments WHERE
ref_id = ?` then re-INSERTs — sentences cascade via the FK. So
backfills are safe to drive repeatedly. The natural production
shape is to run the worker continuously alongside the chunk-level
`embed` + `summarize` handlers.

## Lazy invalidation discipline

Every persisted row carries the versions that produced it:

| Row | Version columns |
|-----|-----------------|
| `ref_segments` | `segmentation_version`, `extractor_version`, `embedder_name` |
| `ref_segment_sentences` | `sentence_splitter_version` (parent row's columns apply transitively) |

**Read-time rule.** If any version on a stored row differs from
the current module constant
(`precis.utils.segmentation.SEGMENTATION_VERSION`,
`precis.workers.segment_toc.EXTRACTOR_VERSION`,
`precis.utils.sentences.SENTENCE_SPLITTER_VERSION`), treat the row
as cache-miss, recompute via the worker, overwrite.

**Write-time rule.** The worker always writes the current values
on every row. Never write a stale version.

**Bump-when rule.** Bump a version constant when a change to that
module's output would invalidate downstream rows:

- `SEGMENTATION_VERSION` — boundary-picking logic (DP cost function,
  K-bounds, boilerplate-filter heuristics).
- `EXTRACTOR_VERSION` — keyword scoring (distinctiveness λ, MMR,
  RAKE candidate cap, KeyBERT changes), sentence picking, forms[]
  flattening.
- `SENTENCE_SPLITTER_VERSION` — anything that shifts `char_offset`
  values: pysbd upgrades, switching splitter library, custom
  abbreviation list edits.

A change to one constant does not require bumping the others —
the invalidation is per-column.

## Boilerplate skip policy

`pipeline._retag_references` writes `chunk_kind='references'` on
the citation-list rows at ingest. Workers extending
`WorkerHandler.skip_chunk_kinds` exclude them from claim:

```python
class EmbedHandler(WorkerHandler):
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = ("references",)
```

`segment_toc` filters the same way at the claim SQL — references
chunks are excluded from the ref-level "do you have body chunks?"
check.

**When to add a kind to `skip_chunk_kinds`:** any chunk whose text
is *noise* relative to the artifact you produce. Bibliographies on
embeddings → noise (low cosine to topic centroid, pollute search).
Bibliographies on RAKE → noise (citation phrases are not useful
keywords). Figures on segment-toc → not noise (caption text is
meaningful), so don't filter them.

## Excerpts are triage, not citations

The `- excerpt @ ~N: "..."` sub-lines in TOC and search-result rows
are *navigational* — they help an agent decide whether to drill in.
They are **never citation-grade**. Discipline:

- TOC view: top-1 sentence by stored `centroid_score`
  (segment-prototypical).
- Search view: top-1 sentence by pgvector cosine against the
  query embedding (query-aligned).
- Citation view (`view='bibliography'` aggregator, future): pulls
  from `kind='citation'` refs whose `meta.source_handle` resolves
  into this paper's chunk range. Each citation's `source_quote` is
  the verbatim text the verifier confirmed; never trust an
  excerpt sub-line as a citation source.

See [`precis-citation-help`](../../src/precis/data/skills/precis-citation-help.md)
for the write-side workflow.

## What the worker does NOT compute (today)

The MVP simplifications, documented for future-you:

- **`aliases[]` on keyword records always empty.** Lemma + cosine
  collapse is a follow-up; today the GIN-indexed `forms` array
  still hits any surface form via the per-paper abbreviation legend.
- **`section_class` always NULL.** Per-paper-kind classifier
  (intro/methods/results/discussion/conclusion) is a follow-up.
  Column is nullable; query path is unaffected.
- **No `status='failed'` poison-pill row.** The worker raises on
  failure; the runner catches and logs. Per-ref failure markers
  are a follow-up — today the next worker pass retries from
  scratch.
- **No HNSW on `ref_segment_sentences.embedding`.** Index is a
  non-breaking add when corpus-wide sentence retrieval becomes a
  real query (see ADR 0018 § alternatives D).

## See also

- ADR 0018 — design rationale
- `docs/design/storage-v2.md § Discovery layer` — full schema
- `precis-toc-help` — agent-facing TOC docs
- `precis-citation-help` — verifier-workflow agent surface
- `precis-search-help` — excerpt sub-line discipline
