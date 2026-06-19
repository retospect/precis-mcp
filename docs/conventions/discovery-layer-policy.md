# Discovery layer — policy

> **F20 rewrite (2026-06-05).** The persistent `ref_segments` /
> `ref_segment_sentences` tables this doc used to govern were dropped
> (migration `0003_drop_legacy_segments.sql`); the `segment_toc`
> worker and its version-column invalidation are gone. Discovery is
> now **per-chunk KeyBERT** stored directly on `chunks`. See
> [ADR 0018](../decisions/0018-persistent-discovery-layer.md) (status
> note) and `src/precis/workers/chunk_keywords.py`. The read/write
> and boilerplate-skip policy below is the current discipline.

When to read keywords, when to recompute, what the worker assumes,
and where the lazy-invalidation discipline applies. Companion to
[`docs/design/storage-v2.md`](../design/storage-v2.md) (§"Discovery
layer", carrying its own F20 amendment banner).

## What the layer is now

- `chunks.keywords TEXT[]` — canonical lower-cased short/display
  forms, GIN-indexed for lexical filter and for Jaccard-distance
  clustering at query time.
- `chunks.keywords_meta JSONB` — versioned envelope with
  `{version, embedder, keywords:[{short, long, score}]}` (KeyBERT
  cosine scores against the chunk's bge-m3 embedding, abbreviations
  resolved via the paper's Schwartz-Hearst legend).

There are no precomputed segment rows and no per-sentence embeddings.

## Read-side policy

**Render from the store, cluster at request time.** The paper TOC
view (`view='toc'`) reads `chunks.keywords` directly and DP-clusters
them per request via `precis.utils.toc_db.render_from_store`. Chunks
below the worker's `_MIN_CHUNK_CHARS` carry empty keyword arrays and
fold into neighbours through the empty-keyword Jaccard defence — no
"segments not yet computed" placeholder.

**Skill kind (and any future TOC-capable file kind) still uses the
in-memory `precis.utils.toc.render` path** (per-request DP+KeyBERT,
memoised per `(slug, scope)` since skill files are static for the
life of the process). Coexistence is by *kind*, not for the same
ref — no drift risk.

Search-result rows no longer carry indented `excerpt @ ~N` sub-lines
(removed with F20). Citation-grade source quotes come from
`kind='citation'` refs, never from a discovery-layer excerpt — see
[`precis-citation-help`](../../src/precis/data/skills/precis-citation-help.md).

## Write-side policy (worker invocation)

Drive the worker via `precis worker --only chunk_keywords`, or let it
run as part of the default round-robin / `--profile=system` pass.
The claim shape is
`keywords IS NULL OR keywords_meta->>'version' != <KEYWORDS_VERSION>`,
so the worker picks up fresh chunks and re-claims any chunk whose
stored version is stale. It commits per chunk; backfills are safe to
drive repeatedly. Production shape: run continuously alongside the
chunk-level `embed` + `summarize` handlers (all three are on the
system profile).

## Lazy invalidation discipline

One constant governs the layer:
`precis.workers.chunk_keywords.KEYWORDS_VERSION`, mirrored into
`keywords_meta->>'version'` on every row.

- **Read-time:** consumers trust whatever version is stored; the TOC
  renderer does not gate on version.
- **Write-time:** the worker always stamps the current
  `KEYWORDS_VERSION`.
- **Bump-when:** bump `KEYWORDS_VERSION` when a change to the
  worker's output would invalidate stored keywords — RAKE candidate
  generation, the abbreviation-resolution step, KeyBERT/scoring
  changes, or the `keywords_meta` envelope shape. Bumping re-claims
  every existing chunk on the next pass (lazy, corpus-wide backfill).

## Boilerplate skip policy

`pipeline._retag_references` writes `chunk_kind='references'` on the
citation-list rows at ingest. The `chunk_keywords` worker skips a
configured non-content set (cards, tables, figures, equations,
references) — those would pollute the keyword set. The chunk-level
`embed` / `summarize` workers skip `references` the same way via
`WorkerHandler.skip_chunk_kinds`:

```python
class EmbedHandler(WorkerHandler):
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = ("references",)
```

**When to skip a chunk_kind:** any chunk whose text is *noise*
relative to the artifact you produce. Bibliographies on embeddings →
noise (low cosine to topic centroid). Bibliographies on KeyBERT →
noise (citation phrases are not useful keywords). Figure captions →
meaningful, so don't skip them from keyword extraction.

## What the worker does NOT compute (today)

- **`aliases[]` collapse.** Surface-form folding beyond the
  per-paper abbreviation legend (lemma + cosine collapse) is a
  follow-up.
- **`section_class`.** A per-kind intro/methods/results classifier
  is a follow-up; nothing in the query path depends on it.
- **No per-chunk failure marker.** The worker raises on failure; the
  runner catches and logs; the next pass retries from scratch.

## See also

- ADR 0018 — original (superseded) discovery-layer rationale
- `docs/design/storage-v2.md` §"Discovery layer" — F20-amended schema
- `src/precis/workers/chunk_keywords.py` — the worker (module header
  is the canonical algorithm reference)
- `src/precis/utils/toc_db.py` — request-time TOC clustering
- `precis-toc-help` / `precis-search-help` — agent-facing docs
- `precis-citation-help` — verifier-workflow agent surface
