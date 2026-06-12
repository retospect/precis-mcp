-- 0012_epo_ops_provider.sql
--
-- Backfill the `epo_ops` row in `providers` for clusters whose
-- 0001_initial.sql was applied before the row was added to the
-- sealed greenfield seed (sealed timestamp 2026-06-04, but our
-- first prod migration ran 2026-05-21). Without this row the
-- patent-ingest insert into refs (provider='epo_ops') trips
-- refs_provider_fkey with a ForeignKeyViolation — observed
-- on caspar 2026-06-12.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe, and
-- fresh installs (where the greenfield seed already added the row)
-- no-op cleanly.

INSERT INTO providers (slug, description) VALUES
    ('epo_ops', 'European Patent Office Open Patent Services REST API')
ON CONFLICT (slug) DO NOTHING;
