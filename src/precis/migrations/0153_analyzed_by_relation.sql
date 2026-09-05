-- 0153_analyzed_by_relation.sql
--
-- The attached-models edge (docs/backlog/attached-models-layer.md, axis 3
-- of the multi-scale design mesh): register the `analyzed-by` /
-- `analysis-of` relation pair binding a design/block to an analysis
-- result (a `finding`, later `estimate`) that carries fidelity + validity
-- scope. Enters WITH its first consumer per the enter-with-consumer rule:
-- `CadHandler.link(rel='analyzed-by')` writes the edge and pins the
-- analyzed design version (`{sha, at}` in `links.meta`, matched against
-- the content sha `cad_save` records in `ref_events`), and the
-- `analysis-stale` condition probe (workers/conditions.py) flags drift.
-- Sibling rows `realizes`/`made-by` deliberately NOT minted here — each
-- waits for its own consumer (design-graph-relations.md, revision
-- 2026-09-05).
--
-- No new tables — edges live in `links`. Asymmetric, with an inverse so
-- both directions auto-mirror at read time. The `Relation` Literal in
-- store/types.py is kept in sync with this seed.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('analyzed-by', FALSE, 'analysis-of',
     'Source design/block is analyzed by the target result (finding/estimate '
     'with fidelity + validity scope); links.meta {sha, at} pins the analyzed '
     'design version.'),
    ('analysis-of', FALSE, 'analyzed-by',
     'Source analysis result describes the target design/block.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0153_analyzed_by_relation.sql
