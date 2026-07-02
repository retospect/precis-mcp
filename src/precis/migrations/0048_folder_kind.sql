-- 0048_folder_kind.sql
--
-- ADR 0045 — the `folder` kind: extrinsic single-parent placement for
-- authored artifacts (draft, structure, cad, todo roots, folders).
-- A folder is a plain numeric ref; children are the live refs whose
-- `parent_id` points at it (the column migration 0013 put on every
-- ref — only todo used it until now). No new tables, no new columns:
-- subtree reads are a recursive CTE over the indexed column, a move
-- is one column write.
--
-- Boot also auto-upserts kinds rows from the handler registry, but a
-- fresh DB (and the test template) must know the kind before any
-- handler-level insert_ref runs — same pattern as 0031 (draft) /
-- 0041 (cad) / 0042 (structure).
--
-- Forward-only (ADR 0005). Idempotent. Baseline snapshot regenerates
-- at release time (ADR 0031) — do not regen per-feature.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('folder', TRUE, 'Folder',
     'Organizational container (ADR 0045): single-parent placement for '
     'authored artifacts via refs.parent_id and the reserved virtual '
     '`parent` link relation (ADR 0027, generalized). Folders organize '
     'what you MAKE — corpus kinds (paper/cfp) keep their own discovery '
     'layer and stream kinds (memory/alert/job) stay out. Shallow by '
     'policy. See precis-folder-help.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0048_folder_kind.sql
