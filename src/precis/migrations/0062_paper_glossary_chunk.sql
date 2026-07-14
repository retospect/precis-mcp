-- 0062_paper_glossary_chunk.sql
--
-- Register the `card_glossary` chunk kind — the reading-prep loop, slice 1
-- (docs/design/reading-prep-loop.md). The `paper_glossary` worker writes a
-- per-paper inferred reading glossary (clustered terms + one-line definitions)
-- as an embeddable negative-ord card variant at `ord = -1000`.
--
-- Why a `card_*` name: the `chunks_check` constraint requires
-- `ord < 0  <=>  chunk_kind LIKE 'card_%'`, and `chunks.chunk_kind` is FK'd to
-- `chunk_kinds(slug)`. So a derived negative-ord chunk MUST be a registered
-- `card_*` kind. `is_card = TRUE` (like `card_combined`). It embeds via the
-- normal cascade (the embedder skips only `references`) and is reachable in
-- search when a caller opts it into the card-kinds union (`_ord_card_clause`).
--
-- Additive, forward-only (ADR 0005), idempotent. Regenerate the baseline
-- snapshot at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('card_glossary', TRUE,
     'Per-paper inferred reading glossary (clustered terms + one-line '
     'definitions); derived + embeddable, written by the paper_glossary worker '
     'at ord=-1000. See docs/design/reading-prep-loop.md.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
