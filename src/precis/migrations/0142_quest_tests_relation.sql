-- 0142_quest_tests_relation.sql
--
-- Quest dialectic, measurement-ruling edge — the `tests` relation that
-- quest-dossier-dialectic §Mechanism deferred ("the `tests`/`tested-by`
-- relation is deferred to the simulation-step deep-link slice").
--
--   * `tests` (measurement artifact → hypothesis finding) — NEW. The source
--     is the computed `pathway` that executed a hypothesis's pre-registered
--     discriminating experiment; the target is the hypothesis finding whose
--     dialectic block pre-registered it. Minted by the code-only
--     measurement-ruling pass (`quest/rulings.py::mint_measurement_rulings`)
--     when a trusted measurement (structure `meta.barrier_trusted` — catpath
--     trust schema 1-2) matches a pre-registered experiment entry; the edge's
--     `meta.ruling` names the templated ruling finding it minted.
--
-- NOT an evidence edge — no evidence flows. Sim-based rulings settle
-- *internal* hypotheses only and never ground nanopub evidence (that stays
-- papers/patents/EDGAR/datasheets — docs/backlog/
-- computed-pathways-cannot-be-cited-as-claim-evidence.md). The
-- interpretation (support/counter/settle against the pre-registered branch
-- predictions) is the next tick's job and mints its own
-- `supports`/`contradicts` edges; this edge only records *that the
-- experiment ran and what measured it*.
--
-- Direction follows the 0100 (`refines`) / 0126 (`conjunct-of`) / 0135
-- (`motivated-by`) convention: derived node → the node it derives from (the
-- measurement exists because the hypothesis pre-registered it). No inverse
-- is registered, matching the same asymmetric-no-inverse convention; read
-- the other direction with `links_for(direction='in')`.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).
-- Kept in sync with the `Relation` Literal in `store/types.py`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('tests', FALSE, NULL,
     'Source measurement artifact (computed pathway) executed the target '
     'hypothesis finding''s pre-registered discriminating experiment — '
     'quest dialectic measurement-ruling edge; NOT evidence, and no '
     'evidence flows along it (sim rulings settle internal hypotheses '
     'only).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0142_quest_tests_relation.sql
