-- 0058_figure_notes.sql
--
-- Split the figure's one shared document into two, by adding a third body
-- chunk_kind `figure_notes` alongside `figure_vocab` (migration 0057). The
-- shared **vocabulary** is the human-facing, high-level ground truth ("a
-- Sierpinski triangle recursed 3 levels"); the implementation **notes** are
-- the model's private design log (element ids, subdivision scheme, opacity
-- conventions) — needed for consistent edits, noise to the human, so they
-- render behind a separate "Implementation notes" tab. Both are fed to the
-- model every turn (both are its memory); only the vocab embeds (searchable
-- ground truth), notes are minted `meta.no_index='true'` like `figure_node`.
--
-- Additive and behaviour-preserving: existing figures gain a notes chunk
-- lazily on the first turn/edit that writes one — no backfill.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('figure_notes', FALSE,
     'A figure''s implementation notes — the model''s private design log '
     '(element ids, structural scheme, conventions). Minted meta.no_index='
     'true, never embedded; rendered behind the "Implementation notes" tab.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
