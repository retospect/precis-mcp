-- ===========================================================================
-- 0011_file_kinds.sql — seed file-backed slug kinds.
--
-- The plaintext / markdown / tex handlers exist and store refs via
-- ``Store.insert_ref(kind=…)``, which goes through
-- ``Store._validate_slug_for_kind`` — which queries the ``kinds``
-- table for the kind row. None of these were in the 0001 seed, so
-- every ``put(kind='markdown'|'plaintext'|'tex', …)`` raised
-- ``BadInput: unknown kind`` at the validation gate.
--
-- Surface impact:
--   - In production this means file-kind ingest has never worked
--     via the seven-verb path; the failure was silent because no
--     production agent had exercised these handlers.
--   - In tests it surfaced as ~86 failures across
--     test_markdown_handler.py + test_plaintext_handler.py +
--     test_tex_handler.py once the PG-gated suite started running
--     against the shared precis_test DB.
--
-- All three kinds are slug-addressed (``is_numeric=FALSE``): the
-- slug is the file's content-stable ID derived from its path under
-- the handler's configured root. See
-- ``src/precis/utils/file_id.py`` for the slug derivation rules.
-- ===========================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('markdown',  FALSE, 'Markdown file',
     'Read / write .md / .markdown files under a configured root. '
     'Slug derived from path; lazy re-ingest on stale mtime; block '
     'slugs are content-stable. See src/precis/handlers/markdown.py.'),
    ('plaintext', FALSE, 'Plaintext file',
     'Read / write .txt / .org / .rst files under a configured root. '
     'The shared file-kind base; markdown and tex are subclasses. '
     'See src/precis/handlers/plaintext.py.'),
    ('tex',       FALSE, 'LaTeX file',
     'Read / write .tex files under a configured root. Inherits the '
     'plaintext file-kind machinery; adds tex-aware block parsing + '
     'input-resolution. See src/precis/handlers/tex.py.')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- End of 0011_file_kinds.sql
-- ===========================================================================
