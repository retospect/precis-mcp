# B4 — `precis_add()` entry point + `precis add` CLI

- **Status**: shipped 2026-05-22 (commits 4d6629b → 0003358; `precis_add()` lives in `src/precis/ingest/add.py`, CLI in `src/precis/cli/`).
- **Authors**: Reto + agent
- **Parent plan**: [`pip-merge.md`](./pip-merge.md) §B4
- **Related ADRs**: 0006 (tri-identifier), 0007 (derived queue), 0008 (drop slug)
- **Replaces**: the `Store.ingest_bundle()` path (formal removal in B7)

## Goal

Stand up `precis_add()` — the single ingest entry point that
writes papers directly into the v2 schema with no `.acatome`
bundle intermediary. Make it the function `precis watch` (B5)
and the `precis add` CLI both call.

## Non-goals

- Worker queue consumer (B6).
- `precis watch` / `precis worker` CLIs (B5/B6).
- Deletion of `Store.ingest_bundle()` and the `ingest-bundle*`
  CLIs (B7).
- TOON output format (B10).
- Wiring re-ingest / `--force` (future).

## API surface (locked)

### `precis.ingest.add.precis_add()`

```python
def precis_add(
    input: PrecisAddInput,
    *,
    store: Store,
    embedder: Embedder | None = None,
    chunk_budget: int = DEFAULT_CHUNK_SIZE,
    s2_api_key: str = "",
    crossref_mailto: str = "",
) -> IngestResult:
    """Ingest one paper into the v2 schema.

    Atomic: all DB writes happen in a single transaction; on any
    error the transaction rolls back and no rows are written.
    Idempotent: if any of the input's identifiers already point at
    a live paper, returns ``IngestResult(inserted=False, ref_id=...)``
    without re-extracting.
    """
```

> **Implementation note (post-B5 fix-up)**: the "without
> re-extracting" promise above is satisfied for PDF inputs by the
> fast-path probe on `pdf_sha256` documented in
> `docs/design/extract-once.md`. The plan diagram below draws
> Marker before the dedup probe — that was the B4d shape and is
> still correct for content-hash / DOI / arxiv-only collisions
> where the identifier is *produced by* extraction. The fast path
> sits one stage earlier so the common "same file re-ingested"
> case no longer pays the Marker cost.

`PrecisAddInput` is a tagged union:

```python
@dataclass(frozen=True)
class PdfInput:
    pdf_path: Path

@dataclass(frozen=True)
class DoiInput:
    doi: str

@dataclass(frozen=True)
class ArxivInput:
    arxiv_id: str

PrecisAddInput = PdfInput | DoiInput | ArxivInput
```

`IngestResult`:

```python
@dataclass(frozen=True)
class IngestResult:
    ref_id: int
    inserted: bool                # False if any identifier already known
    paper_id: str                 # canonical hash-based ID (ADR 0006)
    pub_id: str | None            # arxiv:XXXX | doi:YYYY (None if neither)
    cite_key: str                 # human-readable, with collision suffix
    pdf_sha256: str | None        # None if input wasn't a PDF
    content_hash: str | None      # canonical text hash; None if no PDF
    chunks_written: int           # 0 if inserted=False
    identifiers: dict[str, str]   # {id_kind: id_value} written to ref_identifiers
```

### CLI: `precis/cli/add.py`

```
precis add FILE.pdf                  # ingest a PDF
precis add --doi 10.1038/s41567-...  # metadata-only ingest (no PDF stored)
precis add --arxiv 2401.12345        # metadata-only ingest (no PDF stored)
```

Flags (all optional):
- `--corpus-dir PATH` — copy the PDF into `<dir>/<letter>/<cite_key>.pdf`
  on success. Default: skip the copy. (B5's `precis watch` will set this.)
- `--no-fetch` — disable the CrossRef/S2 lookup cascade; trust embedded
  metadata only. Useful for offline / sealed-corpus runs.
- `--force` — *deferred*; B4 hard-codes `inserted=False` short-circuit.

Exit code: 0 on success (whether `inserted=True` or `False`), non-zero on
failure. Stdout: a single line of `<cite_key> <ref_id> <inserted>`.

## Pipeline (the new flow)

```
PdfInput / DoiInput / ArxivInput
            │
            ▼
   parse_input()                  # PDF: marker.extract + meta sidecar
                                  # DOI/arXiv: skip PDF, fetch meta only
            │
            ▼
   resolve_identity()             # lookup cascade:
                                  #   filename DOI → embedded DOI →
                                  #   arXiv → CrossRef → S2 → fallback
            │
            ▼
   dedupe_identity()              # query ref_identifiers for ANY known ID;
                                  # short-circuit if hit
            │
            ▼            (only if no hit)
   store_pdf()                    # if PDF: INSERT INTO pdfs ON CONFLICT
                                  # DO NOTHING (content_hash + pdf_sha256
                                  # are dedupe keys)
            │
            ▼
   store_ref_atomic()             # one tx:
                                  #   - cite_key resolution (probe
                                  #     ref_identifiers for prefix collisions)
                                  #   - INSERT INTO refs RETURNING ref_id
                                  #   - INSERT INTO ref_identifiers
                                  #     (paper_id, pub_id, cite_key, doi,
                                  #      arxiv, s2, pdf_sha256,
                                  #      content_hash, …)
                                  #   - INSERT INTO chunks (one per chunk;
                                  #     block_ids stays empty for now)
            │
            ▼
   build_result()                 # return IngestResult
```

The derived queue is **implicit**: `chunks` rows with no matching
`chunk_embeddings` row are "pending embedding". The B6 worker will
claim them with `LEFT JOIN ... WHERE chunk_id IS NULL`. We do **not**
INSERT into a job-queue table from `precis_add` (per ADR 0007).

## Module layout

```
src/precis/ingest/
  add.py            # NEW   public entry: precis_add() + dataclasses
  db_writer.py      # NEW   private: the v2 INSERT cascade + cite_key probe
  pdf_metadata.py   # vendored + stripped (B4b)
  pipeline.py       # NEW   replaces acatome_extract.pipeline; emits a
                    #       parsed-paper dataclass for db_writer.write()
src/precis/cli/
  add.py            # NEW   argparse subcommand wiring
```

`db_writer.py` is intentionally **not** a Store mixin. It's a
private function module that takes a `Connection` (or `Store`) and
performs the writes. Reasons:

1. The legacy `_*_ops.py` mixins target the v1 schema and are
   currently broken (B1 deferred their rewrite to B7).
2. Keeping the v2 writer separate means B7's "delete legacy
   mixins" is a clean delete with no extraction step.
3. The writer's API surface is small (one `write_paper()` call)
   and doesn't benefit from mixin composition.

## Slicing (commits)

| # | Slice | Files | Tests | LOC |
|---|---|---|---|---|
| B4a | `db_writer.py` foundation | new module | unit tests against fresh_db fixture | ~400 |
| B4b | vendor + strip `pdf_metadata.py` | bundle reads → `ref_identifiers` queries | ported tests + new ones | ~600 |
| B4c | new `pipeline.py` | replaces acatome's pipeline | unit tests with stubbed marker | ~300 |
| B4d | `precis_add()` + CLI + integration tests | new `add.py` modules | end-to-end with real PG | ~500 |

Each slice keeps tests green. B4a has no consumers yet — it's
exercised by its own unit tests via the existing `fresh_db`
fixture. B4d wires everything together.

## Idempotency contract (concrete)

For each input, before writing any row, the writer probes every
plausible identifier against `ref_identifiers`:

```sql
SELECT ref_id, id_kind FROM ref_identifiers
WHERE (id_kind, id_value) = ANY(VALUES
    ('doi',          :doi),
    ('arxiv',        :arxiv_id),
    ('s2',           :s2_id),
    ('pubmed',       :pubmed_id),
    ('pdf_sha256',   :pdf_sha256),
    ('content_hash', :content_hash),
    ('paper_id',     :paper_id)
)
LIMIT 1
```

A hit returns `IngestResult(inserted=False, ref_id=hit.ref_id, ...)`
with the existing identifiers re-fetched for the result. No
modification to the existing row.

A miss proceeds to the write transaction. Inside the tx, the
`ref_identifiers` PRIMARY KEY (`id_kind`, `id_value`) gives us a
race-safe `ON CONFLICT (id_kind, id_value) DO NOTHING` so a
concurrent ingest can't duplicate.

## `cite_key` collision resolution

`make_cite_key(authors, year, taken=)` returns `surnameYY` plus an
optional `a`/`b`/`c`/… suffix. The writer pre-computes `taken` by
querying:

```sql
SELECT id_value FROM ref_identifiers
WHERE id_kind = 'cite_key'
  AND id_value LIKE :prefix || '%'
```

where `:prefix` is the un-suffixed `surnameYY`. The set of
returned values is passed to `make_cite_key(taken=...)` which
chooses the next free suffix.

Two concurrent ingests of distinct papers with the same `prefix`
race here. Mitigation: the `(id_kind, id_value)` PK rejects the
second writer's duplicate-cite_key INSERT, and the writer retries
the cite_key probe + INSERT once (max 5 retries). After the cap,
raise `CiteKeyExhausted`.

## `pdf_sha256` vs `content_hash`

- `pdf_sha256` — exact-bytes hash of the PDF file. Survives
  metadata-only re-ingests (DOI/arXiv inputs have no PDF, so
  `pdf_sha256` is NULL).
- `content_hash` — canonical hash of normalised text per ADR 0006.
  Survives re-saves of the same paper (different bytes, same text).

Both go into `ref_identifiers` so dedup catches both axes:
- "I downloaded this PDF before" → `pdf_sha256` hit
- "Someone else's PDF of the same paper" → `content_hash` hit
- "Same paper via DOI without a PDF" → `doi` hit

## What B4 explicitly does NOT change

- The legacy `_blocks_ops.py`, `_refs_ops.py`, etc. mixins. They
  remain v1-shaped and tests that depend on them keep failing.
  B7+ rewrites them.
- The MCP server tools. `precis_add()` is reachable via the CLI
  only in B4; an MCP tool wrapper is future work.
- The chunker's algorithm. Same `text_chunker.split_text()` from
  B3; the only change is *where* the output goes (DB rows vs.
  bundle JSON).

## Open questions (to resolve during implementation)

1. **What `actors.slug` does `set_by` get on ingest?** The schema
   FKs `set_by` to `actors(slug)`. B4 will use `'system'` for now;
   if `actors` is empty in a fresh DB, the migration must seed it.
2. **What `kind` does an arXiv-only ingest (no PDF) get?** The
   `kinds` table has `paper`. Same kind, just `pdf_sha256 IS NULL`
   on the ref. Confirmed via the migration's `refs` table — no
   CHECK forbidding a paper without a PDF.
3. **`storage_path` on `pdfs`** — for `precis add file.pdf` without
   `--corpus-dir`, do we store the input path or copy first? B4
   default: store the input path as-is (`Path.absolute().resolve()`).
   B5's `precis watch` will pass `--corpus-dir` and the pipeline
   copies + uses the canonical post-copy path.

## Test plan

- **B4a unit tests** (`tests/ingest/test_db_writer.py`): write a
  ref + identifiers + chunks, assert rows present, assert
  re-write is short-circuited by the dedup query, assert
  cite_key suffix progression on collision.
- **B4b** ports / extends `test_pdf_metadata.py` from
  acatome-extract — strips bundle-specific tests, adds
  ref_identifiers cache-hit / cache-miss tests.
- **B4c** unit-tests the pipeline's data-shape contract with a
  stubbed `extract_blocks_marker` (the real Marker is too heavy
  for unit tests; integration tests in B4d exercise the real one).
- **B4d** integration test: ship a 2-page fixture PDF in
  `tests/fixtures/`, run `precis add` against `fresh_db`, assert
  expected row counts in `refs`, `ref_identifiers`, `chunks`,
  assert second `precis add` of the same file short-circuits.

The `fresh_db` fixture in `tests/conftest.py` already provisions
an ephemeral database per test against the precis-dev container.
