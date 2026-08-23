-- 0135_taproot_motivated_by_relation.sql
--
-- Taproot advisory link, third relation — the motivation edge of a
-- `hypothesis` nanopub. A hypothesis asserts a conjecture and therefore has
-- NO evidence: `nanopub/gates.py::run_mint_gates` rejects a hypothesis that
-- carries grounding passages ("a hypothesis has no supporting passage by
-- definition — motivation, not evidence"). What it carries instead is the
-- set of artifacts that provoked it, written into the signed provenance
-- graph as `prov:wasDerivedFrom` + `precis:motivatedBy`
-- (`nanopub/assemble.py::_provenance`, hypothesis branch).
--
--   * `motivated-by` (hypothesis claim hub → the paper / patent / claim hub
--     that motivated it) — NEW. Direction follows the convention 0100
--     (`refines`) established and 0126 (`conjunct-of`) reused: derived node
--     → the node it derives from. That is also the direction of the
--     artifact's own `prov:wasDerivedFrom`, so the graph edge and the signed
--     bytes point the same way.
--
-- NOT an evidence edge — no evidence flows, exactly as `refines` and
-- `conjunct-of` state. Deliberately distinct from the `HUB_ROLES`
-- (establishes / corroborates / contradicts) paper→hub edges: a motivator is
-- what prompted the conjecture, never support for it. Keeping the two apart
-- is load-bearing, not cosmetic — `workers/hub_refine.py` widens claims by
-- searching for supporting evidence, and pointing that at a conjecture turns
-- it into a confirmation engine (docs/backlog/claim-review-mechanism.md,
-- "Design consequence — widening is motivated retrieval by construction").
--
-- Unlike 0100/0126 the source is a claim hub but the TARGET may be a
-- `paper` / `patent` as well as another `finding` hub, since a hypothesis
-- can be provoked by a passage that was never minted as a claim. The edge
-- is chunk-granular on the source side (`links.src_chunk_id`) the same way
-- evidence edges are, so "which passage provoked this" survives.
--
-- No inverse is registered, matching the asymmetric-no-inverse convention
-- `refines` (0100) / `conjunct-of` (0126) / `establishes` (0094) chose;
-- read the other direction with `links_for(direction='in')`.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).
-- Kept in sync with the `Relation` Literal in `store/types.py`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('motivated-by', FALSE, NULL,
     'Source hypothesis claim hub was provoked by the target artifact '
     '(paper, patent, or claim hub) — taproot advisory link; motivation, '
     'NOT evidence, and no evidence flows along it.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0135_taproot_motivated_by_relation.sql
