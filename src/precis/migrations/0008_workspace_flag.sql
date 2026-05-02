-- Register the ``workspace`` flag name.
--
-- The prose-file handlers (markdown, plaintext, tex) auto-apply this
-- flag to every ref they ingest under ``PRECIS_ROOT``. The LLM uses
-- it to scope ``search(tags=['workspace'])`` to files in its working
-- directory — the LLM sees ``PRECIS_ROOT`` as ``./`` and has no other
-- way to distinguish refs in its workspace from refs that arrived via
-- other paths (external paper imports, etc.).
--
-- Idempotent: ``ON CONFLICT DO NOTHING`` lets the migration run on
-- databases that already have the name (e.g. if an operator added it
-- manually before this migration shipped).

INSERT INTO flag_names (name, description) VALUES
    ('workspace', 'Ref lives under PRECIS_ROOT; auto-applied on ingest for md/txt/log/tex')
ON CONFLICT (name) DO NOTHING;
