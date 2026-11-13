-- 0320_consideration_verdicts.sql
--
-- Consideration verdicts (spec me626852, gr662069): the general
-- "checked, does not fit for this question, at <UTC>, because <reason>"
-- record — usable by any research run, memory or question, not just the
-- quest lit-search (qu164903 / gr592951) that prompted it.
--
-- The scoped memory-or-question → source connection rides the existing
-- link infrastructure as a `considered` edge whose meta carries the
-- CURRENT verdict block (verdict, reason, depth, checked_at, the two
-- endpoint shas). Every judgment also lands one append-only
-- `considerations` row, so canonical-identity dedup on the edge — one
-- (scope, source) pair, one row — never collapses two runs' verdicts:
-- the edge shows the latest, the table keeps them all.
--
-- `verdict`: useful | not-useful | uncertain. `reading_depth` records
-- how far the judge read (none | metadata | abstract | partial |
-- fulltext | passages) — an abstract-only verdict is visible as such.
-- depth='none' is ACQUISITION state (missing text), never a rejection:
-- the write door refuses useful/not-useful at depth 'none'.
--
-- `scope_sha` / `source_sha` pin `precis_target_sha` of each endpoint at
-- judgment time. A consumer treats a verdict as valid only while both
-- still match; a material change on either side leaves the edge stale
-- and queues it for reconsideration — ordinary edits never clear the
-- history itself.
--
-- `link_id` is a plain id, no FK: the edge may be removed while the
-- judgment that wrote it must stay auditable.
--
-- `scope_chunk_id` / `source_chunk_id` are plain ids, no FK either, for
-- the same reason one level down: body chunks are append-only and a
-- re-ingest replaces them (DELETE + INSERT), so an ON DELETE CASCADE here
-- would let a routine chunk replacement delete judgment history. The
-- write door resolves the chunk at judgment time; `scope_sha` /
-- `source_sha` keep the content evidence after the live chunk is gone.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`,
-- `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description)
VALUES
    ('considered', FALSE, NULL,
     'Scope ref (a memory, question or any research node) was judged '
     'against this source: links.meta carries the current verdict block '
     '{verdict: useful|not-useful|uncertain, reason, depth, checked_at, '
     'scope_sha, source_sha}; every judgment also lands a considerations '
     'history row. No inverse: read via links_for(direction=''in'').')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS considerations (
    consideration_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    link_id bigint,
    scope_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    scope_chunk_id bigint,
    source_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    source_chunk_id bigint,
    verdict text NOT NULL
        CHECK (verdict IN ('useful', 'not-useful', 'uncertain')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    reading_depth text NOT NULL
        CHECK (reading_depth IN
               ('none', 'metadata', 'abstract', 'partial', 'fulltext', 'passages')),
    -- A no-text verdict is an acquisition marker, not a judgment.
    CHECK (reading_depth <> 'none' OR verdict = 'uncertain'),
    passages jsonb NOT NULL DEFAULT '[]'::jsonb,
    scope_sha text,
    source_sha text,
    actor text,
    model text,
    run text,
    prompt text,
    set_by text NOT NULL REFERENCES actors(slug) ON UPDATE CASCADE,
    checked_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS considerations_scope_idx
    ON considerations (scope_ref_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS considerations_source_idx
    ON considerations (source_ref_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS considerations_link_idx
    ON considerations (link_id) WHERE link_id IS NOT NULL;
-- The chunk pins are plain columns (no FK — see the header), indexed for
-- history lookups by chunk; set_by is an FK to actors and needs its
-- covering index for parent DELETE/UPDATE scans.
CREATE INDEX IF NOT EXISTS considerations_scope_chunk_idx
    ON considerations (scope_chunk_id) WHERE scope_chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS considerations_source_chunk_idx
    ON considerations (source_chunk_id) WHERE source_chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS considerations_set_by_idx
    ON considerations (set_by);

COMMIT;

-- End of 0320_consideration_verdicts.sql
