-- Phase 9 — register the `patent` kind + EPO OPS providers.
--
-- Patents are read-only refs backed by the EPO Open Patent Services
-- (OPS) API. The handler is hidden when its env vars (EPO_OPS_CLIENT_KEY,
-- EPO_OPS_CLIENT_SECRET, PRECIS_PATENT_RAW_ROOT) are unset, so this
-- migration is safe to apply on machines without OPS credentials —
-- the kind is registered metadata-only and never resolves.
--
-- Tag policy: patents use OPEN lowercase prefixes (cpc:, ipc:,
-- applicant:, country:, kind:, family:, topic:). These are NOT
-- registered in `tag_prefixes` because that table is for closed
-- UPPERCASE axes only (STATUS, PRIO, SRC, CACHE, …). The values
-- live in `ref_open_tags` keyed only by the bare lowercased
-- "prefix:value" string. See store/types.py::Tag.parse.
--
-- Closed-axis whitelist for the `patent` kind ({"SRC", "CACHE"})
-- is added to `_KIND_ALLOWED_AXES` in store/types.py — Python code,
-- not DB state, because the whitelist is enforced by Tag.parse_strict
-- at the agent boundary.

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('patent',  FALSE, 'Patent',
     'Patent record from EPO OPS, addressed by lowercased DOCDB id (e.g. ep1234567b1)')
ON CONFLICT (slug) DO NOTHING;

-- Two new providers:
--   epo_ops         — single-record fetches (biblio/description/claims)
--   epo_ops_search  — search hit-list cache (cache_state rows)
INSERT INTO providers (slug, description) VALUES
    ('epo_ops',
     'EPO Open Patent Services — direct patent fetch (free, fair-use)'),
    ('epo_ops_search',
     'EPO OPS search — cached CQL hit lists (free, fair-use)')
ON CONFLICT (slug) DO NOTHING;
