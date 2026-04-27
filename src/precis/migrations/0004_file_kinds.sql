-- Phase 6 — register file-backed kinds.
--
-- Each is slug-addressed: the ref slug encodes the file's path
-- (relative to that handler's configured root). Block slugs are
-- content-derived hashes so they survive re-ingestion.
--
-- Only `markdown` ships with a working handler in phase 6 session 1;
-- the rest (plaintext, rmk, docx, tex) get registered now so the
-- migration doesn't have to revv each time we land a new handler.
-- The kinds table is purely metadata; an unimplemented kind is
-- harmless (it just doesn't have an entry in the registry).

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('markdown',  FALSE, 'Markdown',  'Markdown document, addressed by file slug'),
    ('plaintext', FALSE, 'Plaintext', 'Plain-text document, addressed by file slug'),
    ('rmk',       FALSE, 'RMK note',  'Reto-flavoured markdown note with front-matter'),
    ('docx',      FALSE, 'DOCX',      'Word document, addressed by file slug'),
    ('tex',       FALSE, 'LaTeX',     'LaTeX document, addressed by file slug')
ON CONFLICT (slug) DO NOTHING;
