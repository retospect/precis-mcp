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

## Resume pointer (2026-08-21) — auth cutover shipped, prod verify pending

The lane's rung-3 failure had TWO root causes stacked. Both fixed; the
end-to-end prod verification is the only open step.

1. **Dead credential** (2026-08-16→21). The vault's `ANTHROPIC_API_KEY`
   was invalid (HTTP 401, last written 07-13), then briefly held an
   `sk-ant-oat…` OAuth token — which the API only accepts as `Bearer`,
   never as `x-api-key`, so it 401'd identically. Reto fixed both slots
   2026-08-21: each now authenticates on its own header (probe returns
   404 model-not-found, i.e. auth passed, on the correct header only).
2. **Wrong auth route.** `diagnose_gripe._spawn_claude` passed
   `bare=True`, and that one flag does double duty: `--bare` forces
   "strictly ANTHROPIC_API_KEY … OAuth and keychain are never read"
   (`claude --help`), AND upstream `call_claude_agent` derives the
   container's secret-by-key channel from it
   (`container_mode = "api" if bare else agent_run_mode()`). Fixed in
   **b87f4b20**: `_restricted_env(..., prefer_oauth=True)` back-fills
   `CLAUDE_CODE_OAUTH_TOKEN` from the vault and scrubs the billed key;
   `bare=not oauth` so the flag tracks the credential. Dropping `--bare`
   re-enables CLAUDE.md/`.claude` hook auto-discovery inside a clone of
   THIS repo, so `_strip_ambient_project_config` deletes them from the
   throwaway ro clone.

Also shipped: **d828c735** — `exit_detail()` in
`utils/_claude_subprocess.py`. `claude -p` prints auth errors to STDOUT,
so the old stderr-only message rendered a bare `exited 1:` and hid this
401 for a full session (gr211457, closed).

**Verify on deployed code — state as of 2026-08-21 18:00Z:**
1. ✅ **Deploy green.** Re-ran after a benign pinned-sha race (see the
   `scripts/deploy` note below); second run was `failed=0
   unreachable=0` on all six hosts with the convergence assert passing,
   so every venv is on the pinned target and the daemons bounced.
2. ✅ **Job 232974 minted** for gripe 209915 (`prio=8`,
   `STATUS:queued`, `idem_key=diagnose:209915`). Minted by calling
   `diagnose_scan._mint` directly — `put(kind='job')` can't express a
   parentless diagnose job (see below). Repeatable recipe, staged at
   `melchior:/tmp/mint-209915.py` (source in the session scratchpad),
   run as:
   `ssh melchior 'sudo -iu deploy bash -lc "PGPASSFILE=/Users/deploy/.pgpass /opt/mcps/venv/bin/python /tmp/mint-209915.py"'`
   It is idempotent — prints `minted=False` and writes nothing if a live
   job already holds the key. **Prod write: hand to Reto, don't self-run.**
3. ✅ **Lane proven end-to-end (2026-08-21 ~18:20Z).** Job 232974
   `STATUS:succeeded`; one `DIAGNOSIS (auto, job 232974):` chunk on gripe
   209915. The diagnosis is *correct*, not merely present: it pins
   `scripts/ship`'s missing option parsing (`--impacted` is the only flag;
   `MSG="${1:-}"` swallows `-m` and drops `$2`), cites the right lines, and
   proposes a `getopts` fix. Corroborated by commit `46b49d0c`, whose
   subject is the literal string `-m`. So rung 2 (report-only diagnosis)
   is live and trustworthy on deployed code.
   Original success criterion, for re-verification:
   `SELECT count(*) FROM chunks WHERE ref_id=209915 AND text
   LIKE 'DIAGNOSIS (auto%' AND retired_at IS NULL`.
   Job status: `SELECT g.namespace||':'||g.value FROM ref_tags rt JOIN
   tags g ON g.tag_id=rt.tag_id WHERE rt.ref_id=232974 AND
   g.namespace='STATUS'`.
   If it FAILS, the failure chunk now carries the real cause —
   d828c735's `exit_detail` puts `claude -p`'s stdout in the message
   instead of a bare `exited 1:`. Read that before theorising.

## Rung 2 is autonomous (2026-08-21 late) — and what now throttles it

After the starvation fix (`f999baea`) + the 16-key sweep, the scanner walked
the whole open-gripe list for the first time: seven consecutive unattended
cycles, `claimed=3 ok=3` each (the worker log's own counter — it read
`claimed=3 ok=0` every cycle before the fix), reaching as deep as gripe
172390. **Converged: 0 left to mint, 22 queued — every open gripe now has
exactly one diagnose job.** Two jobs (233206/233207, gripes 207882/207238)
completed end-to-end with no human in the path: scanner minted, worker
claimed, containerized agent authenticated over OAuth, diagnosis written
back. That is the unattended proof the hand-minted 232974 could not give.

**The remaining bottleneck is NOT the dark factory — it is `news_poll`.**
The `claude_inproc` lane is serial, drains ~4 jobs per pass at roughly
40-minute intervals (~6/hour), and claims in priority order. Queued at the
time of writing:

| job_type | prio | queued |
|---|---|---|
| `news_poll` | 2 | **91** |
| card_forge / meditation / briefing / reading_brief | 2 | 16 |
| `diagnose_gripe` | 8 | 22 |

So ~107 prio-2 rows sit ahead of every diagnosis: ~18 h of lane time. The
diagnoses are not lost (the queue is durable) — they are last in line.
`news_poll` does drain (7 succeeded 20:43), it is simply minted faster than
a 6/hour lane clears it, and at prio 2 it starves every lower-priority
consumer of the lane, not just this one. **Not caused by anything shipped
today; predates it.** Worth its own investigation: why 91 accumulate, and
whether the mint cadence or the lane throughput is the wrong number.

Diagnosis cost is bounded and OFF the dollar meter: since b87f4b20 these run
on OAuth subscription quota, so the ~22 calls do not press the $100/day
ceiling (which was already at $86.61 trailing-24h that night). The breaker
gates the OAuth lane on the quota snapshot instead
(`budget/breaker.py::_gate_quota`) — a NEW dependency the API-key lane did
not have, and worth remembering when a diagnosis lane goes quiet: check
`claude_quota_snapshot`, not just the dollar meter.

**Backlog this uncovered:**
- **17 burned idem-keys.** Every live `diagnose_gripe` job is
  `STATUS:failed`, one per new gripe daily 08-16→08-20. Each burned key
  blocks its gripe from ever being re-diagnosed. Sweep
  (`retired_at=now()`) only AFTER step 3 proves the lane — sweeping
  first risks burning all 17 a second time. Reto authorized a canary
  approach: prove one, then bulk. **Canary passed 2026-08-21 (job
  232974), so the bulk sweep is now unblocked — but it is a prod write:
  prep the SQL and hand it to Reto, don't self-run.** Excludes 210697
  (next bullet). Sweeping frees each key; the next `diagnose_scan` cycle
  re-mints at ≤3/cycle, so the backlog drains over ~6 cycles, not at once.
  Measured 2026-08-21: 17 burned keys, 15 pointing at still-open gripes.
  The starvation fix (above) stops a *future* failure from wedging the
  queue, but does NOT free these already-burned keys — a held key still
  means "diagnosed once, never again" for its own gripe. So the sweep is
  still required, exactly once, to give these 15 their diagnosis.
  Exact sweep (expect `UPDATE 16`):
  ```sql
  UPDATE refs r SET retired_at = now()
  WHERE r.kind='job' AND r.retired_at IS NULL
    AND r.meta->>'idem_key' LIKE 'diagnose:%'
    AND r.ref_id <> 210697
    AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g ON g.tag_id=rt.tag_id
                WHERE rt.ref_id=r.ref_id
                  AND g.namespace='STATUS' AND g.value='failed');
  ```
- **Do NOT sweep job 210697.** Its idem-key is `diagnose:210007`, not
  `diagnose:209915` — and 210007 is the "rung-3 fails" gripe this work
  fixes. Close it (resolution comment naming 89853ce8 entrypoint →
  9e9f12c2 Dockerfile → b87f4b20 OAuth + the key fix), don't re-diagnose it.
- **`fix_gripe` still runs `--bare` on the metered key.** It works again
  now that a real `sk-ant-api…` key is back in the slot, so this is a
  cost optimization, not a breakage. It can't reuse
  `_strip_ambient_project_config` as-is: its clone is read-write and its
  agent commits, so deleting `CLAUDE.md` would land as a spurious
  deletion in the fix commit. Needs its own design pass.
- **Why didn't the scan re-mint 209915? ANSWERED — head-of-line
  starvation, fixed 2026-08-21.** Not a gate problem at all; the scan was
  firing fine. Selection asked `_already_diagnosed` (is there a DIAGNOSIS
  *comment*?) while `_mint` asked the *idem_key*. A **failed** job answers
  no to the first and yes to the second, so its gripe stayed a candidate
  forever — and `_CAP` bounds *candidates selected*, not jobs minted. The
  top 3 failures re-selected every pass (`claimed=3, ok=0`), minting
  nothing and hiding the other ~14 gripes behind them; 209915 at ≈16th was
  unreachable. Fix: `_keys_held()` filters held keys out of *selection* in
  one round-trip, so a failed job costs only its own gripe. Regression test
  `test_held_key_does_not_consume_a_cap_slot` reproduces the wedge
  (verified failing without the skip: `claimed=3, ok=0`).
  **Still true and worth reconciling separately:**
  `PRECIS_DIAGNOSE_SCAN_ENABLED` is **not** in melchior's
  `com.precis.worker.plist` — the pass is armed by the `service_config`
  row from the 08-14 arming, while the module docstring advertises the env
  var. Harmless today (the docstring does explain both layers), but
  `--only diagnose_scan` on a host without the row will mislead.
- **`put(kind='job')` can't express a diagnose job.** The MCP verb
  requires `parent_id` (a todo, or a build subject — a gripe is
  neither), but `diagnose_scan._mint` calls `store.insert_ref` directly
  and mints a *parentless* job. So the sanctioned surface cannot
  reproduce a row the system mints routinely; a hand-mint has to call
  `_mint` (see the 2026-08-21 verify, job 232974). Either teach the verb
  the gripe-subject case or document the gap.
- **`scripts/deploy` exit code is not the deploy's.** It pipes ansible,
  so `$?` is the pipe's — a run with `failed=1` on three hosts still
  exits 0. Always read the PLAY RECAP. Seen 2026-08-21: the first
  redeploy tripped the pinned-sha convergence assert on
  balthazar/melchior/spark because a sibling shipped mid-run; the
  installed sha was a *descendant* of the pinned target (benign race per
  the `deploy-pinned-sha-race` memory), but the assert aborts the play,
  so the daemon bounces after it never ran — new code on disk, old code
  resident. Fix is ff + re-run, and the re-run is what actually matters.
