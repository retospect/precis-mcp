-- 0238_structure_pi.sql
--
-- Structure workspaces slice 2e (docs/backlog/structure-workspaces.md
-- §Structure fisheye — Shared π electrons): per-structure π perception,
-- content-keyed. ``structure_pi`` holds, per ``(ref_id, pi_key)``, the
-- perceived rings / conjugated systems / per-atom membership JSONB the
-- structure eye reads — computed at every ``structure_save`` (import,
-- op, relax write-back), never by the eye.
--
-- ``pi_key`` = sha256(structure_sha · sorted bonds): ``structure_sha``
-- is bond-independent by design, so the bond list is folded in — a bond
-- order change with geometry fixed yields a new key while structure_sha
-- (and the relax cache key) does not. A re-save of identical geometry +
-- bonds is a no-op (ON CONFLICT DO NOTHING); old keys stay addressable.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS structure_pi (
    ref_id       bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    pi_key       text NOT NULL,
    -- {rings: [[ordinals]], systems: [{id, atoms, aromatic_rings}],
    --  atom: {<ordinal>: {rings, system}}, check: {...}}
    data         jsonb NOT NULL,
    perceived_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, pi_key)
);

COMMENT ON TABLE structure_pi IS
    'Per-structure π perception (structure-workspaces 2e): rings, '
    'conjugated systems and per-atom membership keyed by pi_key = '
    'sha256(structure_sha · sorted bonds). Written by structure_save; '
    'read by the structure/se eye and the pi: selector.';

COMMIT;

-- End of 0238_structure_pi.sql
