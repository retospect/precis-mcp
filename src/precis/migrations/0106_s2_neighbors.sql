-- 0106_s2_neighbors.sql
--
-- Paper viewer Sources/Cited tabs (docs/proposals/paper-viewer-nav.md
-- slice 3): today `backfill/citation_lens.py` fetches a paper's full
-- Semantic Scholar reference list + cited-by list, resolves each
-- neighbour against `ref_identifiers`, and keeps only the held↔held
-- subset as a `cites` link edge — the ~90% of a bibliography that isn't
-- (yet) held in the corpus is fetched and thrown away. This table stores
-- the whole neighbour list so the web reader can render a full
-- Sources/Cited tab (held rows link into the corpus; non-held rows show
-- title/year + a fetch affordance), without minting a stub ref per
-- reference (no ref explosion — a stub is created only on an explicit
-- per-row fetch).
--
-- `direction` is the neighbour's relationship to `ref_id`: 'cites' =
-- this paper's outgoing bibliography (S2's `references`), 'cited_by' =
-- papers citing this one (S2's `cited_by`). `ord` is the position in the
-- S2 list — for 'cites' this approximates bibliography order (S2 doesn't
-- guarantee it, but it's the best ordering signal available).
-- `held_ref_id` is resolved against `ref_identifiers` at write time
-- (DOI / S2 id); NULL means we don't hold that paper. No `arxiv` column:
-- `ingest/citations.py::citations()`'s row shape is
-- `{title, doi, year, s2_id}` — S2's reference/citation fields never
-- carry an arXiv id, so there's nothing to store there today.
--
-- Refresh semantics: the writer (`citation_lens.py`) DELETEs all rows
-- for `(ref_id, direction)` then INSERTs the fresh list on every fetch —
-- idempotent per refresh, no unique-collision handling needed for
-- `s2_id` (a neighbour can appear twice in a list, or lack an id
-- entirely). Rides the *same* `citation_edges` ref_event 30-day TTL
-- `citation_lens.py` already stamps — no new event kind, no extra S2
-- calls.
--
-- Forward-only (ADR 0005). Idempotent (`IF NOT EXISTS`). Regenerate the
-- baseline snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS s2_neighbors (
    ref_id      bigint      NOT NULL
        REFERENCES refs (ref_id) ON DELETE CASCADE,
    direction   text        NOT NULL
        CHECK (direction IN ('cites', 'cited_by')),
    ord         int         NOT NULL,
    s2_id       text,
    doi         text,
    title       text,
    year        int,
    held_ref_id bigint
        REFERENCES refs (ref_id) ON DELETE SET NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, direction, ord)
);

-- "Who links to held paper X via S2's graph?" — the read side of a
-- future incoming-neighbour rollup; partial since most rows are non-held.
CREATE INDEX IF NOT EXISTS s2_neighbors_held_ref_id_idx
    ON s2_neighbors (held_ref_id)
    WHERE held_ref_id IS NOT NULL;

COMMIT;

-- End of 0106_s2_neighbors.sql
