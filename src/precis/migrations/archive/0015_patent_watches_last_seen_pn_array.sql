-- ===========================================================================
-- 0015_patent_watches_last_seen_pn_array.sql — fix column type.
--
-- Migration 0014 created ``patent_watches.last_seen_pn`` as ``TEXT``,
-- but the DAO (``src/precis/handlers/_patent_watch_db.py``) writes
-- it via ``unnest(last_seen_pn || %s::text[])`` and reads it as a
-- Python list — i.e. it expects ``TEXT[]``. With ``TEXT`` the DAO
-- stored each pass's IDs as a Python repr string, and the union
-- read back as a per-character set ('[', '"', 'e', 'p', ...).
--
-- 0014 just shipped (≈ minutes ago); only the dev cluster + the
-- test DB carry it, so any data already written is throwaway. We
-- coerce via USING NULL — the runner re-populates from EPO on the
-- next pass.
-- ===========================================================================

ALTER TABLE patent_watches
    ALTER COLUMN last_seen_pn TYPE TEXT[] USING NULL;

-- ===========================================================================
-- End of 0015_patent_watches_last_seen_pn_array.sql
-- ===========================================================================
