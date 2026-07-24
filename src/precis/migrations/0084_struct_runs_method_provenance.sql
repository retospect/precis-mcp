-- 0084_struct_runs_method_provenance.sql
--
-- ADR 0053 §4 — external DFT catalyst data (e.g. OC20/OC22, Materials
-- Project) is imported as an ordinary `structure` design plus a pre-filled
-- `struct_runs` row carrying the source's energy, so it slots into the same
-- run-cube (0043) the relax pipeline writes. Two additions make that safe:
--
--   * provenance — 'computed' (our relax/NEB/MD pipeline, the prior default)
--     vs 'external' (energy came from the source dataset, not from us). An
--     imported row must never be mistaken for one we computed.
--   * method     — nullable fingerprint (functional, cutoff_eV, kmesh, spin,
--     pseudopotentials, dataset_doi, ...) for external rows, so a PBE-DFT
--     energy is never naively compared to our MACE/GPAW number at a
--     differently-converged fidelity. Computed rows leave this NULL — their
--     method is already `model` + `params` (0043).
--
-- Cache-collision guard (ADR 0053 §4): struct_runs_cache_idx (0044) is a
-- plain (non-unique) lookup index, most-recent-first per cache_key, so an
-- external and a computed row for the same geometry already coexist as
-- distinct rows with no constraint violation. The real risk is semantic,
-- not a DB conflict: the cache-first relax lookup must never silently treat
-- an imported external row as a valid cache hit for a computed request
-- (their method fingerprints aren't guaranteed comparable even when
-- structure_sha/fidelity happen to match). Narrow the partial index to
-- provenance = 'computed' so external rows are structurally excluded from
-- ever serving a compute cache hit; they remain fully queryable by ref_id
-- (struct_runs_ref_idx, 0043) same as before.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE struct_runs
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'computed'
        CHECK (provenance IN ('computed', 'external')),
    ADD COLUMN IF NOT EXISTS method     jsonb;

COMMENT ON COLUMN struct_runs.provenance IS
    'ADR 0053 §4: computed (our relax/NEB/MD pipeline) vs external (energy '
    'sourced from an imported dataset, e.g. OC20/Materials Project).';
COMMENT ON COLUMN struct_runs.method IS
    'ADR 0053 §4: method fingerprint for external rows (functional, '
    'cutoff_eV, kmesh, spin, pseudopotentials, dataset_doi, ...). NULL for '
    'computed rows, whose method is already model + params (0043).';

-- Narrow the cache-hit index to computed rows only — an external import can
-- never be returned as a cache hit for a computed relax request.
DROP INDEX IF EXISTS struct_runs_cache_idx;
CREATE INDEX IF NOT EXISTS struct_runs_cache_idx
    ON struct_runs (cache_key, id DESC)
    WHERE cache_key IS NOT NULL
      AND status = 'succeeded'
      AND provenance = 'computed';

COMMIT;

-- End of 0084_struct_runs_method_provenance.sql
