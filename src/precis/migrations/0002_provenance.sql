-- ===========================================================================
-- 0002_provenance.sql — retraction / amendment monitoring infrastructure.
--
-- Phase 1 of docs/design/provenance-kind-plan.md. The refs.retraction_* columns
-- were provisioned in 0001_initial.sql; this migration adds the link
-- vocabulary for notice references and the Retraction Watch provider.
-- The RW dataset cache tables land in a later migration (Phase 3).
-- ===========================================================================


-- Link relations for notice attachment.
--
-- One row in `links` per notice: a paper retracted in 2022 with a
-- correction in 2020 has two outbound edges (retracted-by → notice_r1,
-- corrected-by → notice_c1). The `refs.retraction_status` column carries
-- the dominant status as a query index; the links table is the full
-- chronology. See provenance-kind-plan.md §"Multiple notices on one paper".
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('retracted-by',         FALSE, 'retracts',             'Source is retracted by target (retraction notice)'),
    ('retracts',             FALSE, 'retracted-by',         'Source retracts target'),
    ('corrected-by',         FALSE, 'corrects',             'Source is corrected by target (corrigendum/erratum/addendum)'),
    ('corrects',             FALSE, 'corrected-by',         'Source corrects target'),
    ('concern-raised-by',    FALSE, 'raises-concern-about', 'Source has an Expression of Concern attached'),
    ('raises-concern-about', FALSE, 'concern-raised-by',    'Source raises concern about target');


-- Retraction Watch dataset provider. The RWDB is distributed under CC-BY
-- via Crossref since December 2023; the monthly sync job (Phase 3) will
-- populate a local cache table and join reason codes into the report.
-- Adding the provider row now so Phase 1 notice ingest can reference it
-- without a second migration.
INSERT INTO providers (slug, description) VALUES
    ('retraction_watch', 'Retraction Watch dataset (CC-BY via Crossref)');
