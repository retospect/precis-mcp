-- 0289_code_chunk_kind.sql
--
-- gr592769 (2026-11-08): a draft "reproduce" section listing exact MCP
-- calls had no verbatim code chunk kind, so the author fell back to a
-- paragraph with bullets. `code` was already a handled kind everywhere
-- except the chunk_kinds table: the LaTeX renderer dispatches it to the
-- verbatim lstlisting block (export/latex.py code_kinds), docx prints it
-- in Consolas (export/docx.py code_kinds), the web inline editor edits
-- its source (precis_web/routes/drafts.py _EDITABLE_KINDS), and the
-- narrate/audio pass skips it — but it was never registered, so
-- put(chunk_kind='code') died on chunks_chunk_kind_fkey, the same shape
-- as run_log (0120).
--
-- The write path treats `code` as verbatim (handlers/draft.py): the
-- payload lands as ONE chunk (no blank-line split), skips the
-- dangling-handle gate and the prose lint hints, and mints no
-- mention/link edges from its text; the hygiene view never scans it.
-- The language tag rides meta.language and the LaTeX export passes a
-- listings-known name to \begin{lstlisting}[language=…]. Sibling of
-- 'listing' (0031), which keeps its legacy write-path behaviour.
--
-- is_card FALSE: body content, not a search card. Forward-only
-- (ADR 0005). Idempotent. Regenerate the baseline snapshot at release
-- time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('code', FALSE,
     'Draft code block — verbatim payload, language tag in '
     'meta.language, one chunk per block: no blank-line split, no prose '
     'lint or link minting inside (gr592769). Renders as a code listing '
     'on export; sibling of ''listing'' (0031).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0289_code_chunk_kind.sql
