-- 0179_llm_call_log_placement_routed.sql
--
-- Record where the router MEANT a call to run, next to where it ran.
--
-- `llm_call_log.placement` (0112) is the rung that actually ran — the landed
-- placement, which the dollar caps need. It cannot answer "how much traffic
-- that should have gone local fell back to cloud": a local rung 0 that was
-- skipped (not served on this host), saturated (busy slot → hosted retry) or
-- failed (failover walk) lands as 'cloud' with nothing saying it was meant to
-- be local. The local-vs-cloud share report needs both numbers per tier.
--
-- `placement_routed` is rung 0 of the chain after the operator-intent filters
-- (a strict `placement=` pin, the cloud throttle), classified like `placement`,
-- and 'local' whenever this host declares a `served_by` slot for that rung's
-- model — busy or free, since a busy slot is local capacity the call was
-- headed for. It is set before any mechanical fallback, so
-- `placement_routed = 'local' AND placement = 'cloud'` is exactly the
-- fell-back-to-cloud count.
--
-- Nullable; NULL = a pre-0179 row or a writer outside the router. Not
-- backfilled: an old row's intent was never recorded and is not derivable.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS placement_routed text;

COMMENT ON COLUMN llm_call_log.placement_routed IS
    'local | cloud for the rung the router chose before any fallback '
    '(router._routed_placement). Compare with placement (the rung that ran) '
    'for the routed-vs-landed split. NULL = pre-0179 or a non-router writer.';

COMMIT;
