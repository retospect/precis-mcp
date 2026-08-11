# Infra-failure classification gaps — two paths skip the `infra:child-killed` tag

Two failure paths record a job `STATUS:failed` **without** an
`INFRA_FAILURE_TAGS` open tag, so `_is_infra_failure`
(`src/precis/handlers/_job_bubble.py`, tag-only — reads
`INFRA_FAILURE_TAGS = {swept:claim-orphaned, infra:child-killed}`, never
`meta.failure_class`) mis-classes them as *content-class* failures. A
content-class failure latches `child-failed:<jobid>` on the parent todo
**immediately**, with **no** bounded `orphan_retry_count` retry — the opposite
of what an infra failure should get.

1. **`poison_guard`** (`src/precis/workers/executors/_common.py`) — the
   crash-loop / reclaim-churn cap — sets `meta.failure_class="infra"` but stamps
   **no** open tag. Since `_is_infra_failure` reads tags only, a poison-capped
   job is treated as content-class.
2. **`precis_pathway.seed_job._submit`**'s `except Exception` branch — a
   submit-time crash calls `ctx.record_failure(...)` with **no** `open_tag=`, so
   it too latches content-class.

**Fix:** have `poison_guard` also stamp the `infra:child-killed` open tag (not
just `meta.failure_class`), and pass `open_tag="infra:child-killed"` from
`seed_job._submit`'s crash path. (Alternatively, teach `_is_infra_failure` to
also honour `meta.failure_class="infra"` — one lever covering the poison_guard
case, but not the tag-less `_submit` path.)

**Why low-urgency / why separate from the seed-repair ship:** the automatic
seed-repair path shipped alongside this note (`quest/compute.py`
`_stuck_seed_failure` + the seed-lane retry ladder) keys off job **status**, not
classification, so a mis-classified seed still heals regardless — these gaps
don't wedge the qu164903 class of bug. They're a *visibility/latch* axis: fixing
them means a genuinely infra-killed job bounded-retries via
`orphan_retry_count` instead of latching `child-failed:` on the first hit.
Surfaced by the seed-repair root-cause investigation (2026-08-11). Executor-layer
change — deliberately not bundled into the quest-layer seed-repair fix to keep
that blast radius tight.
