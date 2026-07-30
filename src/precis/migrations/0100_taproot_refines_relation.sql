-- 0100_taproot_refines_relation.sql
--
-- Taproot claim→claim advisory link (ADR 0073 amendment 2026-07-30). A
-- second, sharper wording of an already-minted claim gets minted as its OWN
-- hub (its own pub_id / fi<id>) and linked to the original with a `refines`
-- edge — link, don't merge. This is NOT an evidence edge: no evidence flows
-- between the two hubs (each keeps its own paper→hub edges); the link is
-- read-time advisory only — the fisheye Claims ring surfaces it so the next
-- editor sees "a sharper version of this claim exists" (or "this sharpens an
-- earlier claim").
--
--   * `refines` (sharper claim hub → coarser claim hub) — NEW. Directed
--     from the newer/sharper `TAPROOT:claim` `finding` hub to the one it
--     refines. Both endpoints are claim hubs — endpoint kinds (finding→
--     finding) plus the single write-door guard (`taproot.hub.link_claims`)
--     distinguish it from the paper→hub evidence edges.
--
-- No inverse is registered, matching the asymmetric-no-inverse convention
-- `establishes` (0094) / the 0085 evidence relations chose. The reader
-- (`seniority.derive_refines`) walks both directions with direct
-- `src`/`dst` SQL rather than `store.links_for`, deliberately keeping the
-- `links_for` inverse-rewrite trap (documented in
-- `seniority._fetch_evidence_rows`) out of reach for this slug.
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
    ('refines', FALSE, NULL,
     'Source claim hub is a sharper/reworded version of the target claim hub '
     '(taproot claim→claim advisory link; link-don''t-merge, no evidence flow).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0100_taproot_refines_relation.sql
