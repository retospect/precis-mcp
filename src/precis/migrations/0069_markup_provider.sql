-- 0069_markup_provider.sql
--
-- Seed the `markup` provider slug for markup-first ingest
-- (docs/design/markup-first-ingest.md). Refs whose body chunks come
-- from structured full text (JATS / Elsevier XML / arXiv HTML / LaTeX)
-- instead of Marker OCR are written with provider='markup'; the precise
-- source format lives in refs.meta->>'source_format'. Without this row
-- the markup ingest insert into refs trips refs_provider_fkey.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running and fresh installs
-- (which pick up the row from the greenfield seed) no-op cleanly.

INSERT INTO providers (slug, description) VALUES
    ('markup', 'Structured full-text ingest (JATS / Elsevier XML / arXiv HTML / LaTeX)')
ON CONFLICT (slug) DO NOTHING;
