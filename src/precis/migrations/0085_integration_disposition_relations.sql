-- 0085_integration_disposition_relations.sql
--
-- The integration ledger (paper-writing pipeline rung 2 —
-- docs/design/paper-writing-pipeline.md §"The integration ledger").
-- `integrated-into` is not a new table — disposition rides the existing
-- refs↔refs `links` edge as its relation. Registers the four disposition
-- relations a paper's edge to its topic dossier (a `draft`) can carry:
--
--   * `cited-in`      — woven into the document + a `citation` exists.
--   * `corroborates`  — supports an existing point, grouped with it.
--   * `superseded-in` — subsumed by a later/review paper already woven.
--   * `off-topic-for` — considered and rejected as out of scope.
--
-- Direction is always `paper --relation--> dossier draft` (src=paper,
-- dst=dossier). An optional section anchor rides the edge as
-- `dst_chunk_id` (the section heading chunk), expressed by callers via
-- the standard `draft:<slug>~<selector>` target grammar — no new
-- src-anchor plumbing needed.
--
-- All four are asymmetric *with no inverse* (like `see-also` /
-- `touched`) — a single physical row per edge, no auto-mirror at read
-- time. Dossier→papers traversal is `links_for(dossier, direction='in',
-- relation=<one of the four>)`. Keep in sync with the `Relation` Literal
-- in `store/types.py`.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('cited-in', FALSE, NULL,
     'Paper is woven into and cited by the document; a citation exists. '
     'src=paper, dst=dossier draft (optionally its section chunk).'),
    ('corroborates', FALSE, NULL,
     'Paper supports an existing point in the document, grouped with it.'),
    ('superseded-in', FALSE, NULL,
     'Paper is subsumed by a later or review paper already integrated; '
     'recorded, not separately woven.'),
    ('off-topic-for', FALSE, NULL,
     'Paper was considered for the document and rejected as out of scope.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0085_integration_disposition_relations.sql
