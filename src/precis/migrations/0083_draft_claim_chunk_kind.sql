-- 0083_draft_claim_chunk_kind.sql
--
-- gripe 57812 — `put(kind='draft', chunk_kind='claim', ...)` into a Claims
-- heading (or anywhere) failed with a raw ForeignKeyViolation, both for a
-- single claim and a batched multi-claim write. Root cause: `chunks.chunk_kind`
-- carries an FK to `chunk_kinds(slug)` (0001_initial.sql), and 'claim' was
-- never registered there — the draft handler passes `chunk_kind=` straight
-- through to the INSERT with no allow-list of its own, so an unregistered
-- kind surfaces as an ugly internal DB error rather than a clean BadInput.
-- Users had to fall back to chunk_kind='paragraph'.
--
-- 'claim' is exactly the same shape as 0031_draft_kind.sql's 'table' /
-- 'aside' / 'listing' / 'term' additions — a draft-authored prose flavour,
-- not a new structural mechanism (the existing `patent_claim` chunk_kind is
-- for *ingested* patent text and belongs to a different pipeline; this is
-- the author-facing sibling for a hand-written Claims/claims-style section).
-- No other wiring needed: it falls into the LaTeX export's generic
-- "paragraph and friends" branch and `draft_regex`'s non-derived (plain
-- find/replace-able) bucket already; only `wordcount.PROSE_CHUNK_KINDS`
-- needed the addition, made alongside this migration.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('claim', FALSE,
     'Draft claim statement — a discrete assertion under a Claims-style '
     'heading (patent claim drafting or a scientific claim list). Prose '
     'like paragraph; kept distinct so a renderer/reviewer can tell a '
     'claim from ordinary body text.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0083_draft_claim_chunk_kind.sql
