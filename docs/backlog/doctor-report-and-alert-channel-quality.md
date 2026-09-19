---
status: ready
title: fix lane still dark after 08f79a3a (groomer row + melchior container gate + diagnosis backfill); gr225018 residue; worker_logs ts index
prio: low
model: sonnet
---

# gr225018 residue and the worker_logs index follow-up

Residue of the 2026-09-18 rulings on the doctor report and the orphan
invariant. Everything else shipped the same day: doctor-report preamble
strip, truncated-summary rejection, acronym allowlist, orphan-alert
retirement (23d6234b); the `worker_logs` `/logs` view for the doctor
(6686db89); "Needs a human" bullets → `waiting-for:reto` todos under a
builtin asks root (64e97785); the fix lane — `backlog_groom` mints
`fix_gripe` only for `OPEN:auto-fix` gripes, capped, diagnosis in the
brief, prod env exported (367779ca). Ruled and NOT built: gripe-comment
cadence stays as is (Reto: "no" to change-only annotation). Deployed
2026-09-18 ~19:57Z as 08f79a3a on Reto's word (all six hosts). The
needs-a-human arm works (11 todos in the first 15 h); the fix lane does
not — see the first section below.

## The fix lane is still dark (found 2026-09-19, 15 h after the deploy)

Zero `backlog_groom` passes, zero `fix_gripe` jobs, zero `OPEN:auto-fix`
gripes since the deploy. Three stacked causes:

1. **The groomer's env switch is inert.** `backlog_groom` is an
   `enable_env`-only registry entry; since the §L control cutover such a
   pass registers but runs only with a `service_config` row
   (`cli/worker.py` `_profile_default_on`: the env flag is no longer a
   default source). `diagnose_scan` runs because its row was hand-set at
   the 2026-08-14 arming. 367779ca exported `PRECIS_BACKLOG_GROOM_ENABLED=1`
   and armed nothing. **Landed 2026-09-19:** the precis_worker role's §L
   seed loop now seeds `backlog_groom` prio 5 on a gateway with
   `precis_fix_lane_enabled` (ON CONFLICT DO NOTHING, so a console
   override survives), the decoy env export is removed, and
   `test_seed_loop_arms_the_groomer_row_on_an_armed_gateway_only` pins
   the gate. Takes effect on the next deploy. To arm it now without a
   deploy (prod write, Reto's call): web Status → Services, or
   `precis service prio melchior backlog_groom 5`.
2. **Every post-deploy diagnosis cancels on the container gate.** All four
   `diagnose_gripe` jobs since the deploy (205 of 594 all time) end
   `STATUS:cancelled` with "no containerized agent path available … —
   PRECIS_AGENT_CONTAINER is not enabled on this host" (gr179498
   fail-closed): melchior's `precis_agent_container_enabled` host_var is
   off (20b: "flip only after dream_agent × container is verified"). So
   `PRECIS_DIAGNOSE_AUTOPROMOTE` never gets a diagnosis to tag. Existing
   thread: gr346813. Not a code change; a host_var flip in the overlay.
3. **Backfill gap.** 124 of the 188 succeeded diagnoses carry
   `meta.confidence >= 0.8` but were written with autopromote OFF, so they
   are untagged, and the lane tags only at diagnosis-write time. Once 1
   and 2 are fixed the groomer still starts from zero. Options: a one-off
   tag pass (open gripes, no `no-groom`, newest succeeded `diagnose_gripe`
   job with confidence ≥ 0.8 → `OPEN:auto-fix`, `set_by='system'`), or
   accept that only new gripes ride the lane. Reto's ruling; the one-off
   is a prod write.

## gr225018 — RULED 2026-09-18 (option 1: keep the invariant, stamp the roots) — executed

Reto's ruling: keep the `meta.rotation_root` invariant; stamp the live
top-level project trees strategic by hand. Done the same day on prod via
`tag(kind='todo', id=…, meta={'tier': 'strategic'})` for 16 roots (se+hexfold
paper `td344088`, Parkinson `td243956`/`td244453`, capability landscape
`td259464`, AIxMAT `td261219`, autocatpath validation paper `td262043`, pcb
preprint `td266176`, seed book `td273157`, glass ball `td328426`, photonic
arm `td330494`, EWOD applicability `td346939`, DARPin hunt `td250137`,
pareto-boxel `td262875`, place+route spec review `td264483`, EWOD-in-oil
sources `td338212`, catalyst poisoning `td261041`).

Deliberately NOT stamped (not rotation units): the 25 `waiting-for:reto`
decision rows of 2026-09-17, the 17 `OPEN:ephemeral` autocatpath aggregate
roots, the 4 `[auto] wait for <doi>` stubs, and ~12 parentless cite/assess
chores (six GROUNDING AUDIT ledgers from 2026-08-24/25, six nano-computer
citation chores of 2026-09-13). Those last ~12 are the true residue of the
gripe: re-parent under a draft root or close. The orphan detector stays
silent (`_NO_ALERT`), so nothing nags about them. Close `gr225018` once the
~12 are re-parented or closed; the comment-18 correction is its record.

## worker_logs index follow-up (from the `/logs` review)

- follow-up: `worker_logs` has only a partial level index and no plain
  `ts` btree, so `/logs?level=INFO` with no host/handler facet scans
  unindexed; add the `ts` index in a forward-only migration when the view
  gets real use.
