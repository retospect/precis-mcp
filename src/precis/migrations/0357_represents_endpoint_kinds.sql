-- 0357_represents_endpoint_kinds.sql
-- gr742966: `represents` is concept -> card (the card forge's direction,
-- "source concept is rendered by target card"), but nothing stopped a
-- create-time `put(kind='anki', link='cn…', rel='represents')` storing the
-- backwards card -> concept edge — invisible to the mastery and
-- cardless-concept reads, which only walk concept ->represents-> card and
-- card ->represented-by-> concept. Seed the endpoint kinds on the 0180
-- constraint columns so the link doors' `check_relation_constraints`
-- refuses a `represents` whose source is not a concept or whose target is
-- not a card; the description doubles as the refusal's why and carries the
-- card-side spelling. `represented-by` needs no row of its own: writes of
-- the passive spelling are stored as `represents` with the ends swapped
-- (`_canonical_edge`), and the validator reads the constraint through the
-- inverse slug.
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

UPDATE relations
   SET domain_kinds = ARRAY['concept'],
       range_kinds  = ARRAY['anki'],
       description  = 'Source concept is rendered by target Anki card; '
                      'from the card side the '
                      'same edge is spelled ''represented-by''.'
 WHERE slug = 'represents';

COMMIT;
