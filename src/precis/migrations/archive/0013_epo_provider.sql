-- ===========================================================================
-- 0013_epo_provider.sql — seed the EPO OPS provider for patent ingest.
--
-- ``ingest_patent`` (in ``src/precis/handlers/_patent_ingest.py``)
-- writes ``provider='epo_ops'`` on every patent ref. The provider
-- slug wasn't in the 0001 seed, so any patent ingest raised a
-- ``refs_provider_fkey`` violation.
--
-- Surface impact: silent in production (no operator had ingested
-- a patent end-to-end), surfaced as ~63 test failures across the
-- patent test files once the PG-gated suite started running.
-- ===========================================================================

INSERT INTO providers (slug, description) VALUES
    ('epo_ops', 'European Patent Office Open Patent Services REST API')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- End of 0013_epo_provider.sql
-- ===========================================================================
