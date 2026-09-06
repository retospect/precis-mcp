-- 0156_realizes_relation.sql
--
-- `realized-by` / `realizes` — the last of the four design-graph edges
-- from the 2026-09-04 multi-scale design session (docs/backlog/
-- design-graph-relations.md): a block → the thing that makes it real.
-- First consumer: CadHandler's catalog-part sync (`part <name>
-- <family>:<code>` lines resolve to procurable `component` refs;
-- links.meta.catalog marks the rows the sync manages, so hand-authored
-- candidate realizations are never pruned). Many candidate realizations
-- per block are legal by design.
--
-- Same idempotent-seed pattern as 0153/0154/0155. Forward-only (ADR 0005).

BEGIN;

INSERT INTO relations (slug, inverse_slug, description) VALUES
    ('realized-by', 'realizes',
     'Source ref is made real by the target (e.g. a cad design''s catalog part -> the procurable component that realizes it)'),
    ('realizes',    'realized-by',
     'Source ref makes the target real (e.g. a procurable component -> the design that calls for it)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0156_realizes_relation.sql
