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

NB (2026-08-16, post-4dff45dd): `diagnose_gripe`/`fix_gripe` are
capability-routed on `{claude_bin, git, clones_dir, claude_config_mount}`,
and `clones_dir` only advertises where `PRECIS_FIX_WORK_DIR` is exported in
the worker daemon env. Step 2 was armed on 2026-08-14 (service_config row,
actor "arm dark-factory rung 2") WITHOUT that env, so every scan-minted
diagnose job soft-fallback-routed and died at `load_config_from_env`
(gripe 210007). Fixed 2026-08-16: 20b's `_l_b_fix_env` + the
`precis_worker_agent` provision tasks now carry the fix-lane env + a
deploy-refreshed repo clone, gated on `precis_fix_lane_enabled` (gateway
host_vars — flipped for the gateway that day). Each gripe's diagnosis
idem-key (`diagnose:<id>`) is burned by its failed job — soft-delete the
failed `diagnose_gripe` jobs so the scan re-mints (done for the 08-14→16
casualties in the same session).

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
