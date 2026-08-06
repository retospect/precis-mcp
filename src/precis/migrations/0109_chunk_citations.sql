-- 0109_chunk_citations.sql
--
-- Consumer slice of the citation-resolution work
-- (docs/proposals/citation-taproot-resolve.md; base slice
-- citation-bib-parse.md shipped `paper_bib_entries` in 0108). The base
-- slice turns each held paper's bibliography into structured
-- `marker -> fields -> DOI -> held_ref_id` rows; this table records where
-- those markers are *used inline* in the body — a body chunk carrying
-- `[126]`, `[129,130]`, `<sup>[126]</sup>` maps that marker to the parsed
-- bib entry it refers to. That lets taproot's `resolve_citation` answer
-- "what paper does this claim's `[N]` actually cite?" and lets hub-refine
-- follow a claim's own citation to verify against the cited paper.
--
-- `chunk_id` is the body chunk the marker appears in (`chunks.chunk_id`).
-- `marker` is the numeric-bracket citation number (denormalised from the
-- referenced `paper_bib_entries` row so `resolve_citation(chunk_id,
-- marker)` is a single-index lookup). `bib_entry_id` FKs
-- `paper_bib_entries.id` (the base slice's surrogate PK) — the parsed
-- entry this marker resolves to. Unique per `(chunk_id, marker)`: a chunk
-- that repeats `[126]` yields one row (the `bib_mark` sweep's
-- `ON CONFLICT DO NOTHING` dedupes within a pass).
--
-- Populated by the versioned `bib_mark` sweep
-- (`workers/bib_mark.py`, `BIBMARK_VERSION`): it scans body chunks of
-- papers that already have `paper_bib_entries` rows, extracts inline
-- markers, and keeps only numbers that exist as a parsed bib marker for
-- that paper (the false-positive guard) — swept chunks carry a
-- `BIBMARK:<version>` chunk tag (same drain-and-converge done-marker idiom
-- as `chase_trigger`'s `CHASETRIG:<version>`), so a bump re-sweeps the
-- corpus lazily. Note the one-way coupling: a `BIB_PARSE_VERSION` bump
-- re-mints `paper_bib_entries.id`s (base slice `_delete_stale_entries`),
-- which `ON DELETE CASCADE`s these rows away — so a bib re-parse should be
-- paired with a `BIBMARK_VERSION` bump to repopulate. The reverse holds
-- freely: `BIBMARK_VERSION` can bump alone (e.g. a marker-regex change)
-- without touching `bib_parse`.
--
-- `id` is a plain surrogate PK for a stable single-column handle,
-- matching the sibling `paper_bib_entries` convention.
--
-- Forward-only (ADR 0005). Idempotent (`IF NOT EXISTS`). Regenerate the
-- baseline snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS chunk_citations (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id     bigint      NOT NULL
        REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    marker       int         NOT NULL,
    bib_entry_id bigint      NOT NULL
        REFERENCES paper_bib_entries (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chunk_citations_chunk_marker_uniq UNIQUE (chunk_id, marker)
);

-- Reverse lookup + the FK's own maintenance (a `paper_bib_entries` delete
-- cascades through here): "which inline uses point at this bib entry?".
CREATE INDEX IF NOT EXISTS chunk_citations_bib_entry_id_idx
    ON chunk_citations (bib_entry_id);

COMMIT;

-- End of 0109_chunk_citations.sql
