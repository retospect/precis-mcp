-- 0186_taproot_explains_relation.sql
--
-- Taproot claim→claim advisory link, fourth relation — the causal split
-- (docs/backlog/taproot-compound-migration.md, "`explains` — the causal split",
-- decided 2026-10-03). "X because Y" says one fact (X) and its explanation
-- (Y). The original sentence stays the composite hub, both atoms are its
-- `conjunct-of` parts (0126), and `explains` carries the structure between
-- them.
--
--   * `explains` (explaining claim hub Y → explained claim hub X) — NEW.
--     Directed from the mechanism/cause atom to the atom it explains —
--     "derived-from-the-text node → the node it bears on", the same
--     source-is-the-finer-node convention 0100 (`refines`) and 0126
--     (`conjunct-of`) use. Both endpoints are claim hubs (finding→finding);
--     the single write-door guard (`taproot.hub.link_claims`) enforces the
--     endpoints, refuses a self-link and refuses an edge that would close an
--     explains cycle.
--
-- NOT an evidence edge — no evidence flows along it, ever: `explains` never
-- feeds `derive_evidence`, trust, or the cite fallback (an explanation is not
-- support for the thing it explains; the two atoms each keep their own
-- paper→hub edges). Advisory, like `refines` / `conjunct-of`.
--
-- No inverse is registered, matching the asymmetric-no-inverse convention
-- `refines` (0100) / `conjunct-of` (0126) / `motivated-by` (0135) chose; read
-- the other direction with `links_for(direction='in')`. No `relations`
-- constraint columns (0180) are seeded: like `conjunct-of`, the endpoint rule
-- lives at the write door, not on the generic link doors.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).
-- Kept in sync with the `Relation` Literal in `store/types.py`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('explains', FALSE, NULL,
     'Source claim hub is the mechanism/cause that explains the target claim '
     'hub (taproot claim→claim advisory link from an "X because Y" '
     'decomposition; no evidence flow).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0186_taproot_explains_relation.sql
