-- 0201_nanopub_report_definition_types.sql
--
-- Two new, non-empirical nanopub artifact types: 'report' (a named source
-- states P: "a 2014 conference report by Canatu states ...") and
-- 'definition' (what a term means: "schwarzites are called ...").
-- Reto ruled both on 2026-10-02 (review-queue/answered/nanobuds-paper-13);
-- the gate shape is src/precis/nanopub/gates.py (approve-only type, the
-- epistemic pair replaced by report/definition checks, rdf:type
-- precis:Report / precis:Definition).
--
--   * `nanopub_publish.artifact_type` (0128, renamed by 0181) carries the
--     value and a CHECK over it: drop the CHECK, re-add it as
--     ('claim', 'composite', 'hypothesis', 'report', 'definition'). Widening
--     only: every existing row already satisfies the new set.
--
--   * `nanopub_artifacts.artifact_type` (0128) is unconstrained TEXT and
--     needs no change.
--
-- Forward-only (ADR 0005). Idempotent: DROP IF EXISTS, then the same CHECK.

BEGIN;

-- CHECK widening has no ALTER form; drop + re-add (superset, NOT VALID).
-- squawk-ignore ban-drop-constraint
ALTER TABLE nanopub_publish DROP CONSTRAINT IF EXISTS nanopub_publish_artifact_type_check;

ALTER TABLE nanopub_publish
    ADD CONSTRAINT nanopub_publish_artifact_type_check
    CHECK (artifact_type IN ('claim', 'composite', 'hypothesis', 'report', 'definition'))
    NOT VALID;

COMMIT;

-- Validate outside the rewrite's transaction (squawk
-- constraint-missing-not-valid); the new set is a superset of the old one,
-- so this scan of a few hundred rows cannot fail.
ALTER TABLE nanopub_publish
    VALIDATE CONSTRAINT nanopub_publish_artifact_type_check;

-- End of 0201_nanopub_report_definition_types.sql
