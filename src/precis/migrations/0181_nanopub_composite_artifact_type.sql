-- 0181_nanopub_composite_artifact_type.sql
--
-- Rename the nanopub artifact type 'compound' to 'composite'
-- (docs/backlog/vocab-align-to-literature.md, tier 2: the un-split bundling
-- claim is a "composite" claim; "compound" collides with the chemistry sense
-- in this corpus). The taproot half shipped without a migration; this is the
-- nanopub half.
--
--   * `nanopub_publish.artifact_type` (0128) carries the value and a CHECK
--     over it: drop the CHECK, rewrite the one-off 'compound' rows, re-add the
--     CHECK as ('claim', 'composite', 'hypothesis'). No compatibility path:
--     the old value is rejected after this.
--
--   * `nanopub_artifacts.artifact_type` (0128) is unconstrained TEXT and the
--     table is APPEND-ONLY (trigger), so it is NOT rewritten. Prod holds no
--     artifact row of the old type (the single 'compound' publish row is
--     still a `candidate`, never signed); a signed artifact would carry the
--     IRI `precis:CompoundClaim` in bytes that must never be re-serialised.
--
-- Forward-only (ADR 0005). Idempotent: re-running finds no 'compound' rows
-- and re-creates the same CHECK.

BEGIN;

ALTER TABLE nanopub_publish
    DROP CONSTRAINT IF EXISTS nanopub_publish_artifact_type_check;

UPDATE nanopub_publish
   SET artifact_type = 'composite'
 WHERE artifact_type = 'compound';

ALTER TABLE nanopub_publish
    ADD CONSTRAINT nanopub_publish_artifact_type_check
    CHECK (artifact_type IN ('claim', 'composite', 'hypothesis'))
    NOT VALID;

COMMIT;

-- Validate outside the rewrite's transaction (squawk
-- constraint-missing-not-valid); the UPDATE above left no 'compound' row,
-- so this scan of a few hundred rows cannot fail.
ALTER TABLE nanopub_publish
    VALIDATE CONSTRAINT nanopub_publish_artifact_type_check;

-- End of 0181_nanopub_composite_artifact_type.sql
