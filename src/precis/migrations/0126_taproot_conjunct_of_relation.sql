-- 0126_taproot_conjunct_of_relation.sql
--
-- Taproot claim→claim advisory link, second relation (docs/backlog/
-- taproot-atomic-claims.md). A compound claim hub (e.g. "carbon
-- nanomaterials have exceptional mechanical, optoelectronic, and
-- physicochemical characteristics...") decomposes into atomic claim hubs,
-- each minted as its OWN hub and linked back to the compound with a
-- `conjunct-of` edge — link, don't merge, mirroring `refines` (0100).
--
--   * `conjunct-of` (atom claim hub → compound claim hub) — NEW. Directed
--     from the derived/finer `TAPROOT:claim` `finding` hub to the coarser
--     compound hub it is one conjunct of — same direction convention 0100
--     established for `refines` (derived node → coarser node). Both
--     endpoints are claim hubs — endpoint kinds (finding→finding) plus the
--     single write-door guard (`taproot.hub.link_claims`) distinguish it
--     from the paper→hub evidence edges.
--
-- No inverse is registered, matching the asymmetric-no-inverse convention
-- `refines` (0100) / `establishes` (0094) / the 0085 evidence relations
-- chose.
--
-- All hub/claim-link writes go through the single taproot write door
-- (`src/precis/taproot/hub.py`); a raw INSERT is a defect (ADR 0073 /
-- taproot.md open #16). The DB FK (`links_relation_fkey`) is the durable
-- guard that a typo'd relation never lands.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).
-- Kept in sync with the `Relation` Literal in `store/types.py`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('conjunct-of', FALSE, NULL,
     'Source claim hub is one atomic conjunct of the target compound claim '
     'hub (taproot claim→claim advisory link; link-don''t-merge, no evidence '
     'flow).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0126_taproot_conjunct_of_relation.sql
