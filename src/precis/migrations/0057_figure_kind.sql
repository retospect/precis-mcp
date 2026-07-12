-- 0057_figure_kind.sql
--
-- The `figure` kind — an interactive SVG canvas you draw *with* the model
-- (the "sketch" kind). A chunk-tree document on the SAME substrate as
-- `draft` / `plan` (the mutable `handle`/`pos`/`parent_chunk_id`/
-- `content_sha`/`retired_at` columns + `chunk_events`, added by
-- 0031_draft_kind.sql) and the same DraftMixin store ops — but a **distinct
-- kind** so it is NEVER exported as a corpus deliverable
-- (`corpus_role='none'`; the export guard rejects it). Its rendered raster
-- (later slice) is an *asset* a draft can include, orthogonal to this.
--
-- A figure is TWO documents the model owns: a `figure_node` chunk (the SVG
-- source `<svg>…</svg>`, addressed `fn<id>` — slice 1 keeps the whole
-- document as one node; per-element splitting is a later slice) and a single
-- `figure_vocab` chunk (the shared vocabulary + conventions — "green circles
-- are foos"). Chat turns persist as `figure_turn` chunks so a session is
-- resumable. `figure_node` markup is low-value / high-churn
-- search text so it is minted with `meta.no_index='true'` (the handler) and
-- never embeds; `figure_vocab` + `figure_turn` are prose and DO embed — a
-- search hits the vocabulary, not `<circle>`.
--
-- Unlike `plan`'s 1:1 `plan-of`, a project may own MANY figures, so the
-- `figure-of` relation is many-per-project and figure creation does NOT go
-- through `create_draft`'s 1:1 dup-checked path (the handler uses
-- `insert_ref` + `add_chunks` directly).
--
-- Additive and behaviour-preserving: reuses the existing `chunks` /
-- `chunk_events` structure — NO new tables or columns. Registers the `kind`
-- row, three `chunk_kinds`, and the `figure-of` / `has-figure` relation.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('figure', FALSE, 'Figure',
     'An interactive SVG canvas you draw *with* the model — a slug-addressed '
     'chunk-tree on the draft substrate, addressed by fg<ref>/fn<chunk>. Two '
     'model-owned documents: the SVG source (figure_node chunks) + a shared '
     'vocabulary (figure_vocab); chat persists as figure_turn. NEVER exported '
     'as a deliverable (corpus_role=none). Many per project (figure-of link). '
     'See precis-figure-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. body chunk_kinds ------------------------------------------------
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('figure_node', FALSE,
     'A figure''s SVG source document — the addressable source node (fn<id>). '
     'Raw markup: minted meta.no_index=true, never embedded.'),
    ('figure_vocab', FALSE,
     'A figure''s shared vocabulary + drawing conventions — the negotiated '
     'ground truth ("green circles are foos"). Prose, embedded + searchable.'),
    ('figure_turn', FALSE,
     'One chat turn on a figure (user message + model reply) — the resumable '
     'session log. Prose, embedded + searchable.')
ON CONFLICT (slug) DO NOTHING;

-- 3. the project-binding relation (many figures per project) ---------
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('figure-of', FALSE, 'has-figure',
     'Source figure belongs to target project (todo). Many-per-project.'),
    ('has-figure', FALSE, 'figure-of',
     'Source project (todo) has target figure. Many-per-project.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
