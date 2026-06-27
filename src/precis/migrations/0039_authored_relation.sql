-- 0039_authored_relation.sql
--
-- ADR 0039 — the `authored` / `authored-by` link relation binding a
-- `kind='orcid'` author node to a `kind='paper'` ref. Authorship is a
-- **document-level (ref→ref) edge, never to chunks** — a person authors a
-- paper, not a paragraph. The edge's `meta` may carry best-effort
-- author-position info (`author_position`, `n_authors`) when known (only the
-- S2-mediated path has it; ORCID is per-person and lists no co-authors).
-- "Senior/last author" is an LLM judgement from those, not a stored flag.
--
-- `authored`:    src = author (orcid), dst = paper.
-- `authored-by`: inverse — src = paper, dst = author.
-- Asymmetric with a documented inverse, so it auto-mirrors at write time
-- (store._INVERSE_RELATIONS) and "who wrote this paper?" reads via
-- links_for(paper, relation='authored-by', direction='out').
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING. Keep the
-- `Relation` literal + `_INVERSE_RELATIONS` map in store/types.py in sync.
-- Regenerate the baseline snapshot at release (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('authored', FALSE, 'authored-by',
     'Source author node (kind=orcid) authored the target paper. '
     'Ref-level edge; meta carries best-effort author_position / '
     'n_authors when known (ADR 0039).'),
    ('authored-by', FALSE, 'authored',
     'Source paper was authored by the target author node (inverse of '
     'authored).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0039_authored_relation.sql
