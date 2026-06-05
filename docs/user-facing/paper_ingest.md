# Precis V2 — paper ingest

## Architecture

```
PDF  ─[acatome-extract]─►  foo.acatome  ─[precis ingest-bundle]─►  v2 DB
                          (gzipped JSON,
                           stable schema)
```

`.acatome` bundles are the **canonical interchange format**.

- `acatome-extract` (separate package, unchanged) does PDF → bundle.
- `precis.store.ingest_bundle()` does bundle → v2 schema.
- No migration script. No tolerant parser. Same path is used for existing
  v1 bundles AND new papers going forward.

## Bundle shape (v1, unchanged)

```python
{ "header":      { "paper_id", "slug", "title", "authors", "year",
                   "doi", "arxiv_id", "journal", "abstract", "s2_id",
                   "entry_type", "keywords", "pdf_hash", "page_count",
                   "source", "verified", "verify_warnings",
                   "extracted_at" },
  "blocks":      [ { "text", "embeddings": {"<profile>": [float, ...]},
                     ... } ],
  "enrichment_meta": { "profiles", "embedding_models", ... } }
```

If the schema ever evolves, add `header.bundle_version`. v2 ingest fails
fast on unknown versions — explicit migration only.

## Mapping → v2 schema

| Bundle field                  | V2 destination               | Notes |
|------------------------------|-----------------------------|-------|
| `header.slug`                | `refs.slug`                 | Preserved verbatim |
| `header.title`               | `refs.title`                | |
| `header.{authors,year,doi,arxiv_id,journal,abstract,s2_id,keywords,entry_type,pdf_hash,page_count,paper_id,verified,verify_warnings,extracted_at}` | `refs.meta` (JSONB) | One blob; PaperHandler views (`bibtex`, `ris`, ...) read from here |
| `header.source`              | `refs.provider` (FK)        | Map `'embedded'→'manual'`, `'crossref'→'crossref'`, `'arxiv'→'arxiv'`, ... |
| `blocks[i].text`             | `blocks.text`               | |
| `blocks[i].embeddings.<active-profile>` | `blocks.embedding`  | Only the profile matching `system.embedding_model` |
| array index `i`              | `blocks.pos`                | 0-based |
| (mint)                       | `blocks.slug`               | NEW — see below |
| (classify)                   | `blocks.density`            | NEW — see below |
| `enrichment_meta`            | discarded                   | Active profile lives in `system` |

## Block slug minting

Bundles have no block slugs. Mint deterministically per `(ref_id, text)`:

```python
def mint_block_slug(ref_id: int, text: str) -> str:
    """5-char base32. Same text under same ref → same slug → re-ingest safe."""
    h = hashlib.sha256(f"{ref_id}\x00{text}".encode()).digest()
    return base64.b32encode(h)[:5].decode().upper()
```

Collision handling: on `(ref_id, slug)` UNIQUE violation, extend by one
char from the hash and retry. With 5 chars of base32 (~33M values) and
~100 blocks per paper, collisions are vanishingly rare.

**Why deterministic-on-content:** re-ingesting a bundle (e.g. after an
embedder upgrade) keeps slugs stable → citations survive re-ingest.
Reordering `pos` is fine; slugs are the citation handle.

## Density classification at ingest

Cheap heuristic; no model. Stored on `blocks.density`. Re-runnable via
`precis jobs sweep-densities`.

```python
def classify_density(text: str) -> Density:
    n_tokens = approx_tokens(text)
    n_digits = sum(c.isdigit() for c in text)
    if n_tokens < 20 or text.count("\n") / max(n_tokens, 1) > 0.15:
        return "sparse"
    if n_digits / max(n_tokens, 1) > 0.10:
        return "dense"
    return "medium"
```

Refine empirically later. Schema doesn't constrain the algorithm.

## What's NOT in bundles

User-added metadata in v1 lived in `acatome-store` postgres, not in
bundles:

- User-applied tags
- Notes / annotations
- Cross-paper links (v1 didn't have these as first-class anyway)

**Default policy for v2 reingest:** drop. Re-tag / re-link as needed.

If a body of user-curated tags/notes is worth preserving, write a
*separate* one-off pg→pg copy script that targets `ref_closed_tags` /
`ref_open_tags` / `links`. Throw-away code, not a permanent migration.

## CLI

```
precis jobs ingest-bundle  <bundle.acatome>      # one file
precis jobs ingest-bundles <dir>                 # walk dir
precis jobs reembed                              # global re-embed (model change)
precis jobs sweep-densities                      # recompute densities
```

Same jobs are used for both initial backfill (point at
`~/.acatome/papers/`) and ongoing per-paper ingest. There is no
"migration mode" — bundle ingestion is just how papers enter v2.

## Idempotency

`ingest_bundle` is idempotent on `(corpus_id, kind='paper', slug)`:

- If ref doesn't exist → INSERT new ref + blocks.
- If ref exists and `pdf_hash` matches → no-op (same source).
- If ref exists and `pdf_hash` differs → REPLACE blocks (re-extracted
  PDF), preserve `refs.id`, preserve user-added tags/links/notes.

Block slugs survive replacement when text is unchanged (deterministic
mint); only changed-text blocks get fresh slugs.

## Re-embed flow (when `system.embedding_model` changes)

Single-author throw-away migration:

1. `precis jobs reembed --new-model <model> --new-dim <dim>`:
   - Begin tx.
   - `UPDATE system SET value = :new_model WHERE key = 'embedding_model'`
   - `UPDATE system SET value = :new_dim WHERE key = 'embedding_dim'`
   - `ALTER TABLE blocks ALTER COLUMN embedding TYPE vector(:new_dim)`
     (postgres requires DROP+ADD or USING expression; in practice we
     `UPDATE blocks SET embedding = NULL`, then re-embed in batches)
   - Bump `system.schema_epoch`.
   - Commit.
2. Background batches of `blocks_missing_embeddings()` → embed → write back.

This is rare enough not to need clever orchestration — just downtime
on semantic search until backfill completes; lexical search keeps
working throughout.
