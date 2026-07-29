-- 0094_taproot_evidence_relations.sql
--
-- Taproot Phase 2 — evidence-edge vocabulary (ADR 0073; design
-- docs/proposals/taproot.md + build ticket
-- docs/proposals/taproot-phase2-hub-node.md). A paper→claim-hub evidence
-- edge carries one of three roles; only ONE new slug is needed:
--
--   * `establishes` (originator paper → claim hub) — NEW. "Source paper
--     first showed / originated the target claim." Directed from a `paper`
--     ref to the `FROLE:claim` `finding` hub. Originators are derived from
--     the citation graph, not hand-set (taproot.md §"Seniority is derived");
--     this relation records the derived verdict.
--
-- The other two roles reuse relations that already exist — endpoint kinds
-- disambiguate the shared slug (the same way this ticket reuses
-- `contradicts` for both paper→hub and hub↔hub):
--
--   * `corroborates` — already seeded (0085, integration-disposition). The
--     NOT-ORIGINATOR supporters (paper supports the claim while citing an
--     earlier originator).
--   * `contradicts`  — already seeded (0001, with inverse `contradicted-by`).
--     Evidence against the claim, and hub↔hub opposite-claim links.
--
-- No inverse is registered for `establishes`: the hub reads its evidence via
-- `links_for(hub, direction='in', relation=<role>)` — direction filtering,
-- not auto-mirroring — so an inverse slug is unnecessary. This matches the
-- asymmetric-no-inverse convention 0085 chose for its evidence relations and
-- avoids perturbing the shared `corroborates` (which 0085 left inverse-less).
-- Kept in sync with the `Relation` Literal in `store/types.py`.
--
-- All hub/edge writes for these relations go through the single taproot
-- write door (`src/precis/taproot/hub.py`); a raw INSERT is a defect
-- (ADR 0073 / taproot.md open #16). The DB FK (`links_relation_fkey`) is the
-- durable guard that a typo'd relation never lands.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('establishes', FALSE, NULL,
     'Source paper first showed / originated the target claim (taproot '
     'evidence edge; originator).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0094_taproot_evidence_relations.sql
