-- 0150_struct_atoms_charge.sql
--
-- gr285775: the structure kind's DRC had two blind spots — no formal charge
-- anywhere, and metal coordination unchecked (unbounded valence). This adds
-- the storage side of the first half: a per-atom DECLARED formal/net charge
-- (intent, same tier as `oxidation`/`hybridization` — NOT the run-scoped
-- DERIVED partial charge `struct_runs.charges` already carries, which is a
-- different, DFT-computed thing). `from_smiles` now carries rdkit's
-- `GetFormalCharge()` in; `add_atom` accepts an explicit `charge`; validate.py
-- rules 2/5 read it to compute a charge-aware valence budget instead of the
-- neutral-only table. NULLable-with-default-0 so an old row loads as neutral
-- (Store.structure_load coalesces NULL -> 0 either way; the column default
-- covers a direct SQL reader too).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE struct_atoms ADD COLUMN IF NOT EXISTS charge smallint NOT NULL DEFAULT 0;

COMMENT ON COLUMN struct_atoms.charge IS
    'Declared formal/net charge (intent) — e.g. a quaternary ammonium N+ or '
    'a carboxylate O-. Distinct from struct_runs.charges (DERIVED per-atom '
    'partial charge from a DFT/ML run). Feeds validate.py''s charge-aware '
    'valence budget (elements._CHARGED_VALENCE, gr285775).';

COMMENT ON TABLE struct_atoms IS
    'ADR 0043 §4/§12: a design''s atoms — intent + current fractional '
    'position, including a declared formal charge (gr285775). Per-atom '
    'DERIVED outputs (force/partial charge) are run-scoped, not here. '
    'Never embedded.';

COMMIT;

-- End of 0150_struct_atoms_charge.sql
