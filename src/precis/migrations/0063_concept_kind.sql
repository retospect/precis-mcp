-- 0063_concept_kind.sql
--
-- The `concept` kind — nodes in the learner's personal knowledge graph
-- (reading-prep loop, docs/design/reading-prep-loop.md; supersedes decision 7's
-- objectives-as-todos). A numeric-id ref (like memory/anki): `title` = the
-- concept name; `meta` carries the continuous `mastery` field + derived `state`
-- + canonical `definition`. The embeddable definition is emitted as the reused
-- `card_combined` chunk (ord=-1, no new chunk_kind) so a concept IS a vector in
-- the corpus manifold (frontier distance / routing get this for free).
-- corpus_role is authored/'none' (never cited as evidence) — enforced by the
-- handler/export guard, not here.
--
-- Also registers the concept-graph link relations (kept in sync with the
-- `Relation` Literal + `_INVERSE_RELATIONS` in store/types.py):
--   has-prerequisite ↔ prerequisite-of  — the learning DAG (asymmetric).
--     `Y has-prerequisite X`  ⇒  learn X before Y.
--   analogy-of                          — symmetric (teach one via the other).
--   contrasts-with                      — symmetric (confusably similar, distinct).
--   represents ↔ represented-by         — concept ↔ each card that renders it.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('concept', TRUE, 'Concept',
     'A node in the learner''s personal knowledge graph (reading-prep loop): a '
     'term/idea with a continuous mastery field, derived state, embeddable '
     'definition, and typed edges (prerequisite / analogy / contrast) to other '
     'concepts. Objectives are concepts, not todos. See reading-prep-loop.md.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('has-prerequisite', FALSE, 'prerequisite-of',
     'Source concept requires target concept first (the learning DAG).'),
    ('prerequisite-of',  FALSE, 'has-prerequisite',
     'Source concept is a prerequisite of (must be learned before) target.'),
    ('analogy-of',       TRUE,  NULL,
     'Source and target concepts are analogous — teach one via the other.'),
    ('contrasts-with',   TRUE,  NULL,
     'Source and target concepts are confusably similar but distinct.'),
    ('represents',       FALSE, 'represented-by',
     'Source concept is rendered by target card (an anki/other representation).'),
    ('represented-by',   FALSE, 'represents',
     'Source card renders (is a representation of) target concept.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
