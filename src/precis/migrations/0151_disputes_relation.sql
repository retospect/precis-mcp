-- 0151_disputes_relation.sql
--
-- Non-blocking disagreement — Part 1 of the `disputes`/`contradicts` split
-- (docs/backlog/disputes-edge-nonblocking-disagreement.md; folded into the
-- owning docstrings on ship). One slug (`contradicts`) previously meant at
-- least four unrelated relationships (evidence-contradicts-claim,
-- claim-contradicts-paper, critique-contradicts-claim,
-- memory-contradicts-memory) and hard-blocked nanopub minting on the
-- strength of unreviewed LLM verdicts and review critiques. Split along
-- who has decided:
--
--   * `disputes` — NEW. "These two artifacts appear to conflict; someone
--     should look." Free to file (agent, human, or LLM judge), never
--     blocks publication, rendered as an open question. Directed
--     (filer's subject → the thing it questions), asymmetric, NO inverse —
--     the `refines` (0100) convention: readers walk both directions with
--     direct src/dst SQL, keeping the `links_for` inverse-rewrite trap
--     (see `seniority._fetch_evidence_rows`) out of reach.
--
--   * `contradicts` — narrowed, not removed. Now adjudication-derived
--     only (Part 2, docs/backlog/disputes-adjudication-workflow.md): a
--     live claim-graph `contradicts` edge blocks publication because an
--     adjudication established the conflict. After Part 1 NO code path
--     writes it; the write doors reject it. `memory`↔`memory` usage is a
--     different subsystem and keeps its vocabulary untouched.
--
-- Backfill (decision D2): every existing claim-graph `contradicts` row is
-- repointed to `disputes` — none was ever adjudicated (no adjudication
-- mechanism has existed), so none carries the warrant the slug now
-- asserts. Census at build (2026-09-02): 5 claim-graph rows (3
-- review-critique finding→finding, 2 finding→paper), 2 memory↔memory rows
-- left untouched, 0 `contradicted-by` rows (the inverse is a read-time
-- rewrite, not a stored row). Claim-graph `contradicts` therefore starts
-- at zero — correct, not a regression.
--
-- All hub/claim-link writes go through the taproot write doors
-- (`src/precis/taproot/hub.py`); a raw INSERT is a defect. The DB FK
-- (`links_relation_fkey`) is the durable guard that a typo'd relation
-- never lands.
--
-- Forward-only (ADR 0005). Idempotent: the seed is ON CONFLICT DO
-- NOTHING; the repoint UPDATE matches zero rows on a re-run (guarded
-- against `links_endpoints_relation_idx` collisions with a NOT EXISTS).
-- Kept in sync with the `Relation` Literal in `store/types.py`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('disputes', FALSE, NULL,
     'Source artifact appears to conflict with the target — a non-blocking '
     'open question, free to file, resolved only by adjudication '
     '(which alone may derive a blocking `contradicts`).')
ON CONFLICT (slug) DO NOTHING;

-- D2 repoint: claim-graph `contradicts` → `disputes`. Excludes
-- memory↔memory rows (different subsystem). The NOT EXISTS guards the
-- unique index (src, src_chunk, dst, dst_chunk, relation) NULLS NOT
-- DISTINCT against a pre-existing identical `disputes` edge.
UPDATE links l
   SET relation = 'disputes'
  FROM refs sr, refs dr
 WHERE sr.ref_id = l.src_ref_id
   AND dr.ref_id = l.dst_ref_id
   AND l.relation = 'contradicts'
   AND NOT (sr.kind = 'memory' AND dr.kind = 'memory')
   AND NOT EXISTS (
        SELECT 1 FROM links d
         WHERE d.src_ref_id = l.src_ref_id
           AND d.src_chunk_id IS NOT DISTINCT FROM l.src_chunk_id
           AND d.dst_ref_id = l.dst_ref_id
           AND d.dst_chunk_id IS NOT DISTINCT FROM l.dst_chunk_id
           AND d.relation = 'disputes'
       );

-- Collision leftovers (a `disputes` twin already existed, so the UPDATE
-- above skipped the row): the `contradicts` row is redundant with its
-- twin and unwarranted under the new semantics — drop it. No-op today
-- (`disputes` is brand new) and on every fresh DB; here for idempotence
-- against partial re-runs.
DELETE FROM links l
 USING refs sr, refs dr
 WHERE sr.ref_id = l.src_ref_id
   AND dr.ref_id = l.dst_ref_id
   AND l.relation = 'contradicts'
   AND NOT (sr.kind = 'memory' AND dr.kind = 'memory')
   AND EXISTS (
        SELECT 1 FROM links d
         WHERE d.src_ref_id = l.src_ref_id
           AND d.src_chunk_id IS NOT DISTINCT FROM l.src_chunk_id
           AND d.dst_ref_id = l.dst_ref_id
           AND d.dst_chunk_id IS NOT DISTINCT FROM l.dst_chunk_id
           AND d.relation = 'disputes'
       );

COMMIT;

-- End of 0151_disputes_relation.sql
