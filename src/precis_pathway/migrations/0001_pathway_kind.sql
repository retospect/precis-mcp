-- 0001_pathway_kind.sql  (plugin namespace: autocatpath)
--
-- The `pathway` kind: a autocatpath reaction-network exploration owned by a
-- slug-addressed ref. Body = the config YAML's methods paragraph
-- (chunk_kind='pathway_body', embedded + citable); graph + pooled-
-- uncertainty results + provenance snapshot live in refs.meta.
--
-- Slice 0 (dark, PRECIS_AUTOCATPATH_ENABLED): runs EMT in-process. Native
-- `structure` refs per intermediate + the `pathway-node`/`has-pathway`
-- relations are slice 1 — deferred here because `Relation` is a closed
-- Literal in precis core (store/types.py), so extending it needs a
-- precis-core edit, not just this plugin migration.
--
-- Forward-only + idempotent (ADR 0005). Runs AFTER the precis-core chain
-- (the migrator orders built-ins first), so `kinds` / `chunk_kinds` exist.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('pathway', FALSE, 'Reaction pathway (autocatpath)',
     'A catalyst reaction-network exploration (autocatpath): a surface + '
     'substrate + target YAML config; relaxed intermediates and NEB '
     'barriers with pooled uncertainty (low-confidence flagged). '
     'Slug-addressed; body is the methods paragraph, graph/results in '
     'meta. Slice 0 runs EMT in-process. See precis-pathway-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the body chunk kind (FK target for the embedded methods chunk) --
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('pathway_body', FALSE,
     'autocatpath pathway methods paragraph — the citable, embedded body of a '
     'pathway ref (deterministic provenance text from autocatpath.provenance).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0001_pathway_kind.sql
