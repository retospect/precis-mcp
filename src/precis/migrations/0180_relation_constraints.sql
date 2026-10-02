-- 0180_relation_constraints.sql
--
-- Relation constraints as data (docs/backlog/relation-constraints.md).
-- `relations` carried a slug, a symmetry flag, an inverse and a
-- description nothing read; every endpoint rule lived as a hand guard on one
-- relation in one handler. These five columns let one validator at the two
-- generic link doors (`handlers/_link_tag_ops.py::check_relation_constraints`)
-- enforce them, mirroring how `Tag.parse_strict` is driven by tag-vocabulary
-- data:
--
--   domain_kinds / range_kinds  allowed `refs.kind` of the link's source /
--                               target. NULL = unconstrained.
--   functional                  at most one live edge of the relation per
--                               target ("one draft per project").
--   transitive                  stored + documented only; drives no
--                               inference in v1.
--   acyclic                     the relation (stored in either direction)
--                               may not close a cycle; checked by the shared
--                               `LinksMixin.ancestors` walk.
--
-- Seeds only relations whose rule is already documented or enforced
-- elsewhere; adding a constraint later is one UPDATE migration. Existing
-- rows that violate a new rule are left alone — the validator gates new
-- writes only. `instance-of` is NOT seeded here (term-taxonomy's item owns
-- it). The evidence-kind set below is `taproot/hub.py::EVIDENCE_SRC_KINDS`
-- plus `PATHWAY_EVIDENCE_KINDS`; `corroborates` also lands paper -> draft
-- (the integration-ledger disposition, 0085), so its range is
-- {finding, draft}, not {finding} alone.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE relations
    ADD COLUMN IF NOT EXISTS domain_kinds text[],
    ADD COLUMN IF NOT EXISTS range_kinds  text[],
    ADD COLUMN IF NOT EXISTS functional   boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS transitive   boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS acyclic      boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN relations.domain_kinds IS
    'Allowed refs.kind of the link source; NULL = unconstrained.';
COMMENT ON COLUMN relations.range_kinds IS
    'Allowed refs.kind of the link target; NULL = unconstrained.';
COMMENT ON COLUMN relations.functional IS
    'At most one live source ref per target for this relation (read through the inverse slug too).';
COMMENT ON COLUMN relations.transitive IS
    'Declaration only: A->B and B->C imply A->C. Drives no inference in v1.';
COMMENT ON COLUMN relations.acyclic IS
    'The relation (in either stored direction) may not close a cycle.';

-- The hand guard on `contradicts` is now data; its message is the relation
-- description, so the old "adjudication-derived, file disputes" advice moves
-- here.
UPDATE relations
   SET domain_kinds = ARRAY['memory'],
       range_kinds  = ARRAY['memory'],
       description  = 'Source contradicts target. Claim-graph contradicts is '
                      'adjudication-derived and cannot be filed manually — '
                      'file rel=''disputes'' instead (free, non-blocking). '
                      'Only memory-to-memory contradicts is fileable.'
 WHERE slug = 'contradicts';

UPDATE relations SET functional = TRUE
 WHERE slug IN ('draft-of', 'plan-of', 'dossier-of');

UPDATE relations
   SET domain_kinds = ARRAY['datasheet', 'edgar', 'paper', 'patent', 'pathway'],
       range_kinds  = ARRAY['finding']
 WHERE slug = 'establishes';

UPDATE relations
   SET domain_kinds = ARRAY['datasheet', 'edgar', 'paper', 'patent', 'pathway'],
       range_kinds  = ARRAY['draft', 'finding']
 WHERE slug = 'corroborates';

UPDATE relations SET transitive = TRUE, acyclic = TRUE
 WHERE slug IN ('specialises', 'contains', 'has-prerequisite', 'serves');

COMMIT;

-- End of 0180_relation_constraints.sql
