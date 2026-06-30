-- 0044_struct_run_cache.sql
--
-- ADR 0043 §23.16 — the run-cube IS the cache. A relax at an energy rung
-- (ml/dft-*) is expensive and deterministic in its inputs, so a request is
-- cache-first: hash the input geometry + fidelity + model + params + code
-- version into a content address (`cache_key`) and, on an exact prior
-- `succeeded` run, return it with ZERO compute — geometry included.
--
--   * structure_sha   — content address of the INPUT geometry (cell + per-atom
--     element/frac/fixed/magmom/oxidation; label- and bond-free). Two designs
--     that share an input share this hash.
--   * cache_key       — sha256 over (structure_sha, fidelity, model, params,
--     code_version). The lookup key; one succeeded run per key is a hit.
--   * final_geometry  — the relaxed positions (canonical-rank-indexed frac +
--     lattice) so a hit can write the geometry back onto ANY design sharing the
--     input, with no CoW-snapshot table (the variant/adopt-as-head story stays
--     separately deferred, ADR §8/§10).
--
-- The cube is append-only and never invalidated (decision A2): a changed
-- geometry hashes to a new key, so a stale hit cannot occur. Bumping the code
-- version (precis.structure.cache.RELAX_CODE_VERSION) retires every prior entry
-- without touching a row — old keys simply stop matching.
--
-- The rung-0 `clean` repair is never cached (instant, pure, no energy), so its
-- runs carry NULL cache columns and the partial index skips them.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE struct_runs
    ADD COLUMN IF NOT EXISTS structure_sha  text,
    ADD COLUMN IF NOT EXISTS cache_key      text,
    ADD COLUMN IF NOT EXISTS final_geometry jsonb;

COMMENT ON COLUMN struct_runs.cache_key IS
    'ADR 0043 §23.16 content address: sha256(structure_sha, fidelity, model, '
    'params, code_version). Lookup key for the cache-first relax; NULL for the '
    'uncached clean rung.';

-- One succeeded run per cache key is a hit. Partial so the uncached clean rung
-- (NULL cache_key) and failed runs never bloat or shadow the lookup. Most-
-- recent-first within a key so the newest succeeded run wins on a hit.
CREATE INDEX IF NOT EXISTS struct_runs_cache_idx
    ON struct_runs (cache_key, id DESC)
    WHERE cache_key IS NOT NULL AND status = 'succeeded';

COMMIT;

-- End of 0044_struct_run_cache.sql
