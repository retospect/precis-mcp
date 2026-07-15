-- 0066_mermaid_kind.sql
--
-- The `mermaid` kind — a mermaid diagram you draw *with* the model (ADR
-- 0057, slice 4). A second instance of the shared diagram core beside
-- `figure`: same draw-with-me turn loop, same element→chunk `depicts`
-- bindings, differing only in the source language (mermaid text, validated
-- + rendered + exported by the pure-Python `mermaidx` engine).
--
-- A slug-addressed ref on the SAME chunk-tree substrate as `figure` /
-- `draft` (the mutable `handle`/`pos`/`parent_chunk_id`/`content_sha`/
-- `retired_at` columns + `chunk_events`, added by 0031_draft_kind.sql) and
-- the same DraftMixin store ops (parameterised `kind='mermaid'`) — but a
-- **distinct kind** so it is NEVER exported as a corpus deliverable
-- (`corpus_role='none'`; the export guard rejects it), addressed by
-- mm<ref>/mn<chunk>.
--
-- Documents the model owns: a `mermaid_node` chunk (the source), a
-- `mermaid_vocab` chunk (shared vocabulary), a `mermaid_notes` chunk
-- (private notes, no_index), and `mermaid_turn` chat chunks. `mermaid_node`
-- + `mermaid_notes` are minted `meta.no_index='true'` (the handler) and
-- never embed; `mermaid_vocab` + `mermaid_turn` are prose and DO embed.
--
-- Like `figure`, a project may own MANY mermaid diagrams, so the
-- `mermaid-of` relation is many-per-project.
--
-- Additive and behaviour-preserving: reuses the existing `chunks` /
-- `chunk_events` structure — NO new tables or columns. Registers the `kind`
-- row, four `chunk_kinds`, and the `mermaid-of` / `has-mermaid` relation.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('mermaid', FALSE, 'Mermaid',
     'A mermaid diagram you draw *with* the model — a slug-addressed '
     'chunk-tree on the draft substrate, addressed by mm<ref>/mn<chunk>. '
     'Model-owned: the mermaid source (mermaid_node) + a shared vocabulary '
     '(mermaid_vocab) + private notes (mermaid_notes); chat persists as '
     'mermaid_turn. Nodes bind to the chunks they depict (ADR 0057). NEVER '
     'exported (corpus_role=none). Many per project (mermaid-of link). See '
     'precis-mermaid-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. body chunk_kinds ------------------------------------------------
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('mermaid_node', FALSE,
     'A mermaid diagram''s source document — the addressable source node '
     '(mn<id>). Minted meta.no_index=true, never embedded.'),
    ('mermaid_vocab', FALSE,
     'A mermaid diagram''s shared vocabulary + conventions — the negotiated '
     'ground truth. Prose, embedded + searchable.'),
    ('mermaid_notes', FALSE,
     'A mermaid diagram''s private implementation notes (node ids, structure, '
     'conventions) — the model''s design log. Minted no_index, not embedded.'),
    ('mermaid_turn', FALSE,
     'One chat turn on a mermaid diagram (user message + model reply) — the '
     'resumable session log. Prose, embedded + searchable.')
ON CONFLICT (slug) DO NOTHING;

-- 3. the project-binding relation (many diagrams per project) --------
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('mermaid-of', FALSE, 'has-mermaid',
     'Source mermaid diagram belongs to target project (todo). Many-per-project.'),
    ('has-mermaid', FALSE, 'mermaid-of',
     'Source project (todo) has target mermaid diagram. Many-per-project.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
