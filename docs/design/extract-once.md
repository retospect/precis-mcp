# Extract once: probe `pdf_sha256` before Marker

**Status**: planned (post-B5 fixup on `feat/storage-v2-step-b`)
**Owner**: `src/precis/ingest/add.py`
**Predecessor**: `docs/design/b4-precis-add.md` (idempotency contract)
**Related ADR**: 0007 (derived queue — embeddings/summaries are
chunk-scoped and unaffected)

## Problem

The B4 plan promised idempotent ingest *without re-extracting*
(`docs/design/b4-precis-add.md:43-45`). The current implementation
honours the contract at the **write** layer — duplicate identifiers
collapse to the existing `ref_id` and no rows are duplicated — but
**not** at the **extract** layer. `precis_add(PdfInput)` always
runs Marker (~30–60 s/PDF on a CPU host, longer when surya OCR
fires) before probing `ref_identifiers`:

```@/Users/reto/work/projects/inbox_code/precis-mcp/src/precis/ingest/add.py:118-141
    paper = _build_paper(             # ← Marker runs HERE
        input,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
    )

    with store.pool.connection() as conn:
        existing = probe_existing(    # ← dedup probe runs AFTER
            paper_id=paper.paper_id,
            doi=paper.doi,
            ...
```

The unit test `tests/ingest/test_add.py::test_dedup_via_pdf_sha256`
pins this behaviour with `side_effect=[first, second]` — Marker is
explicitly expected to be invoked twice on the duplicate path.

### Why this matters in practice

| Scenario | Hits today? |
| --- | --- |
| Watcher steady state (PDF arrives once → moves to corpus) | no — the file leaves the inbox so it can't be re-processed |
| Watcher restart with default `--backfill` | no — successful PDFs are gone, errors live in `errors/` (skipped by `_MANAGED_DIRS`) |
| Manual `precis add` against a corpus PDF | **yes** — 30 s+ of wasted Marker time |
| Same paper from two different bytes (publisher PDF + author preprint) | **yes** for the second file — content-hash dedup catches it, but Marker has already paid |
| Recovery flow ("re-drop everything into inbox") | **yes** for every duplicate |

The user-visible cost is non-trivial on the duplicate paths and
the watcher does not protect us when the operator works outside
the normal "drop into inbox" channel.

## Goal

For PDF inputs, compute the cheap `pdf_sha256` first, probe
`ref_identifiers (id_kind='pdf_sha256')`, and short-circuit
**before** Marker if the hash is already known.

## Non-goals

- **Forced re-extract** (e.g. for Marker model upgrades). Out of
  scope; today the manual path is `DELETE FROM refs WHERE
  ref_id = X` (chunks cascade) followed by re-ingest. A future
  `precis add --force` lands later if demand surfaces.
- **Earlier dedup on `content_hash` / `doi` / `arxiv`**. Those
  identifiers are produced *by* the extract step (content_hash
  from Marker's body text; DOI from the metadata cascade which
  reads sidecar + embedded + lookup). Probing them earlier means
  running the work that produces them — there is no fast path.
- **Restructuring `extract_paper`**. The split between "identity"
  and "extract" lives entirely in `precis_add`; `pipeline.py`
  keeps its current API. Tiny re-hash cost (~1 ms / PDF) is
  accepted in exchange for zero plumbing churn elsewhere.

## Design

Two probes, in two phases. The fast-path probe is bytes-cheap and
catches the common "same file again" case. The existing slow-path
probe stays exactly as it is — it catches the rarer "same paper,
different bytes" case where Marker has already run.

```
PDF on disk
  │
  ▼
read bytes + make_pdf_sha256(bytes)     ◄── ~1 ms
  │
  ▼
probe_existing(pdf_sha256=…)            ◄── 1 DB roundtrip
  │
  ├── HIT  → build IngestResult from existing ref's identifiers
  │          return inserted=False, chunks_written=0
  │          ◄── **Marker never runs**
  │
  └── MISS → extract_paper(pdf_path)    ◄── Marker, metadata cascade
              │
              ▼
            probe_existing(paper_id, doi, arxiv, content_hash, …)
              │
              ├── HIT  → existing slow-path short-circuit
              │
              └── MISS → write_paper() in one tx, commit
```

The fast-path probe runs on a separate, short-lived connection;
we then drop it before Marker so we don't hold a connection for
the duration of extraction. This matters because Marker's runtime
is dominated by model inference, not DB work, and the pool is
sized for many short-lived tx, not a few long ones.

### Public surface

No changes. `precis_add(PdfInput, store=...)` keeps the same
signature and same return type. The acceleration is invisible
unless you measure wall-clock or watch the Marker logs.

### `IngestResult` for the early-probe hit

Today's `_hit_result()` takes a `PaperToWrite` and uses its fields
as fallbacks when `ref_identifiers` is missing rows. For the
early-probe path we don't *have* a `PaperToWrite` yet. Solution:
build the result purely from what we have:

- `ref_id`: the probe's hit.
- `inserted`: `False`.
- `cite_key`, `paper_id`, `pub_id`, `pdf_sha256`, `content_hash`:
  re-fetch from `ref_identifiers` in the same probe connection.
- `chunks_written`: `0` (no writes happened).
- `identifiers`: the full `{id_kind: id_value}` map from the DB.

Refactor: rename `_hit_result(paper, *, ref_id, conn)` →
`_hit_result_from_db(ref_id, *, conn, pdf_sha256_hint=None)`. The
hint is just for completeness in the result (so the caller sees the
sha256 it actually probed with); identifiers always come from the
DB.

The existing slow-path call site updates to call the same helper:

```python
if existing is not None:
    return _hit_result_from_db(existing, conn=conn,
                               pdf_sha256_hint=paper.pdf_sha256)
```

The DoI / arxiv paths also use the same helper. Single code path
for "I found you in the DB, here's your IngestResult".

### File-by-file changes

| File | Change | LoC |
| --- | --- | --- |
| `src/precis/ingest/add.py` | add fast-path probe; rename `_hit_result` → `_hit_result_from_db`; update three call sites | ~50 |
| `tests/ingest/test_add.py` | update `test_dedup_via_pdf_sha256` (assert Marker called **once**, not twice); add `test_pdf_sha256_probe_short_circuits_marker` regression | ~40 |
| `CHANGELOG.md` | one bullet under the existing `## Unreleased` → `### Fixed` (this fix rides the same release as B7-B10 unreleased work) | ~10 |
| `pyproject.toml` | **no bump** — version stays `7.0.0`; the unreleased B7-B10 work will drive the next bump (≥ 7.1.0) at release time | 0 |
| `docs/design/b4-precis-add.md` | append a short "Implementation note" pointing at this doc | ~10 |
| `OPEN-ITEMS.md` | (no entry needed — this *closes* the implicit gap from B4d's contract) | 0 |

Net diff: ~100 lines across 4 active files.

## Thresholds review (`docs/conventions/thresholds.md`)

- **Schema**: untouched. No migration. ✅
- **API**: no CLI flag changes, no subcommand changes, no MCP
  response shape changes. ✅
- **Ingest**: `pdf_sha256` algorithm is unchanged (we just probe
  with it earlier). `cite_key` rule unchanged. ✅
- **Performance**: net win. One extra cheap DB roundtrip on first
  ingest; saves a Marker run on every duplicate. ✅
- **Operational**: no destructive mutation. The change is purely
  additive in the control flow. ✅

No threshold trips. Proceeding without further checkpoint.

## Test plan

1. **Update** `tests/ingest/test_add.py::test_dedup_via_pdf_sha256`:
   the existing assertion `r2.ref_id == r1.ref_id` stays; the
   `side_effect=[first, second]` mock changes to a single
   `return_value=first` — and we add `assert m.call_count == 1`.
   The PDF bytes determine the sha256 that the early probe sees;
   the fixture's `_fixture_paper(pdf_sha256=...)` must agree with
   that sha256 so both probes match the same row. Easiest: have
   the test compute the sha256 from the on-disk bytes via
   `precis.identity.make_pdf_sha256` and feed that into the fixture.
2. **New regression** `test_pdf_sha256_probe_short_circuits_marker`:
   write a ref with a known `pdf_sha256` row directly via SQL,
   then call `precis_add(PdfInput(pdf))` on a file whose bytes
   hash to that same value. Assert `extract_paper` was **not**
   called (`m.call_count == 0`) and `result.inserted is False`.
3. **No new test** for the slow path — the existing
   `TestPrecisAddIdempotent::test_second_call_short_circuits`
   (DOI path) and the other existing tests cover it.
4. **Smoke**: run the full `pytest` sweep inside `precis-dev`;
   confirm coverage on `src/precis/ingest/add.py` does not drop
   below the current baseline.

## Rollout

1. Land this design doc.
2. Ship the patch in one commit titled
   `B5a: probe pdf_sha256 before Marker for true extract-once`.
3. Add a bullet to the existing `## Unreleased` → `### Fixed`
   block in `CHANGELOG.md` describing the user-visible saving
   (~30–60 s per duplicate, no behavioural change for fresh
   ingests). No version bump — the B7-B10 unreleased work drives
   the next release.
4. Rebuild `precis-mcp:latest`.
5. Apply migrations (no-op — schema unchanged).
6. Use the now-fixed pipeline to ingest the 10 user-staged PDFs
   via `precis watch` against a staged inbox.

## Risk

- **Connection churn**: the fast-path probe acquires and releases
  a connection before Marker. On a saturated pool this is a few
  extra waits — but the alternative (hold the connection through
  Marker) is far worse for concurrency. Mitigation: the existing
  `psycopg_pool` is sized for short-lived tx; the extra acquire
  is in the noise.
- **Stale `pdfs.storage_path`**: not introduced by this change,
  but worth a one-line note in OPEN-ITEMS.md (the watcher records
  `storage_path = /inbox/foo.pdf` *before* moving the file to
  `<corpus>/<letter>/<cite_key>.pdf`; the column is therefore
  stale forever). Fixing that is a separate small commit.
