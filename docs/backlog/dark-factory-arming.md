---
status: draft
title: Arm the dark-factory gripe loop (dials + follow-ons) — Reto's call
---

# Arm the dark-factory gripe loop (dials + follow-ons) — Reto's call

The self-fix loop shipped fully dark (diagnose_gripe + diagnose_scan,
fixer gripe-intake lane, canary-staged deploy). Nothing runs until these
dials turn; sequence is bottom-up so each rung proves the one above:

1. `PRECIS_DEPLOY_CANARY=<host>` (deploying machine env; `scheduler`
   recommended) — canary-staged `scripts/deploy`; useful for human `/go`
   immediately, prerequisite for fixer autonomy past `report`.
2. `PRECIS_DIAGNOSE_SCAN_ENABLED` (agent-profile worker host) — open
   gripes start receiving `DIAGNOSIS (auto…)` comments, ≤3/cycle,
   Tier.BIG. Report-only; touches nothing else.
3. Either fix lane, or both (they exclude via gripe STATUS):
   `PRECIS_FIXER_GRIPE_DB=<prod pgbouncer URL>` in the fixer LaunchAgent
   plist (laptop OAuth lane; picks open+`auto-fix`+diagnosed gripes), or
   `PRECIS_BACKLOG_GROOM_ENABLED` (cluster FRONTIER fix_gripe rail).
4. `PRECIS_DIAGNOSE_AUTOPROMOTE=1` — diagnoses ≥0.8 confidence tag
   `auto-fix` themselves, closing the human out of promotion. Turn last.
5. Fixer autonomy `PRECIS_FIXER_AUTONOMY=ship` (then `full`) once
   1–4 have soaked.

Follow-ons parked at build time (all small, none blocking arming):
- Fixer lane write-back: append a timeline note / flip STATUS on the
  gripe when a build lands, so the gripe reflects the pushed branch
  (v1's sink is the report + branch only).
- Canary verify via boot-id epoch (`src/precis/liveness.py`) instead of
  heartbeat freshness — stronger "actually restarted onto new code".
- Re-diagnosis staleness window (v1: one diagnosis per gripe ever,
  idem_key unconditional).
- Spine slice 4 (`doctor_tick` — fleet-level find-issues front-end)
  stays with the self-healing-spine thread; the gripe interface here is
  what it feeds.
