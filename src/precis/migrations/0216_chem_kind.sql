-- 0216_chem_kind.sql
--
-- Register `kind='chem'` — chemical name / CAS / abbreviation resolver.
--
-- get(kind='chem', id='aspirin') resolves an identifier to canonical
-- SMILES + InChIKey (abbreviation table → OPSIN → PubChem, cross-checked
-- against NCI/CIR) and caches the answer in cache_state. Data-only: the
-- kind reuses refs + chunks + cache_state. Two registry rows:
--   * `kinds.chem`     — runtime kind validator
--   * `providers.chem` — cache_state/refs.provider FK target
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('chem', FALSE, 'Chemical identifier resolver',
     'Resolve a chemical name, CAS number or lab abbreviation to canonical '
     'SMILES + InChIKey. Every answer names its source; ambiguous '
     'identifiers are refused with candidates listed. Cached 30 days. See '
     '``precis-chem-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO providers (slug, description) VALUES
    ('chem', 'Chemical identifier resolution (PubChem PUG REST, OPSIN, NCI/CADD CIR)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0216_chem_kind.sql
