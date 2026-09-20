---
status: ready
title: fix lane after 08f79a3a — agent-lane container env (gr346813 fix, needs deploy); brief/doctor quality findings; gr225018 residue; worker_logs ts index
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
   the gate. Takes effect on the next deploy. **Armed by hand 2026-09-19
   21:38Z** on Reto's "as recommended": `service_config` row
   `melchior/backlog_groom prio 5` (actor "reto via claude …"); the seed's
   ON CONFLICT DO NOTHING leaves it alone.
2. **Most diagnoses cancel on the container gate — per PROCESS, not per
   host (gr346813 root cause, found 2026-09-20).** melchior runs four
   worker processes; the dedicated agent lane (`precis-worker-agentlane`,
   playbook 20e, `--only job_claude_inproc --only job_inproc`) wins most
   `claude_inproc` claims and carried NO `PRECIS_AGENT_CONTAINER` (20e's
   env had only the fix dirs), so every diagnose/fix job it won cancelled
   "PRECIS_AGENT_CONTAINER is not enabled on this host" (gr179498
   fail-closed). The collapsed unit (`precis-worker`, 20b) DOES carry it —
   the overlay's `precis_agent_container_enabled: true` was already set —
   and the few jobs it won succeeded (2026-09-20: 2 succeeded on
   `precis-worker`, 8 cancelled on `precis-worker-agentlane`, same
   8.34.0 build). That claim race is the whole "flap". The earlier note
   here ("host_var off") was wrong. **Fix landed 2026-09-20:** 20e's env
   now mirrors 20b's `_l_b_container_env` darwin branch (gate + bin +
   image) and exports `PRECIS_DIAGNOSE_AUTOPROMOTE=1` too (the promote
   decision is read by the process that runs the diagnosis; 20b's export
   never reached the lane), pinned by
   `test_agentlane_carries_the_container_gate_and_autopromote_like_20b`.
   Live on the next deploy; 20e bootout+bootstraps the unit, so the env
   takes (no kickstart trap). Close gr346813 on the deploy.
3. **Backfill — DONE 2026-09-20 09:31Z** on Reto's "tag 14": 14 gripes
   tagged `OPEN:auto-fix` (`set_by='system'`; gr180306 gr182230 gr228652
   gr228699 gr244679 gr248866 gr259665 gr259666 gr263257 gr263258
   gr264778 gr294498 gr311857 gr343755). Skipped 6 of the 20 candidates:
   gr239587/gr239588/gr260308 (ops or overlay work), gr266041 (its own
   text: not a clear-cut bug), gr182078 (feature ask), gr228594 (dup of
   gr228652). The groomer has run hourly since arming (`claimed=0` until
   the tags existed); its 6 h refresh window next opens ~14:30Z, minting
   up to 3 `fix_gripe` todos per window. **Until the 20e fix is deployed
   those mints mostly cancel on the agent lane** (a cancelled fix todo is
   not `done`, so it blocks a re-mint for that gripe — no spin, but no
   fix either). Deploy first if the cancel noise matters.

## Doctor report and morning brief: quality findings (2026-09-20)

- **Brief health line was the bare word "Classification" — FIXED 2026-09-20.**
  `briefing_cast._doctor_report_line` took the report's first paragraph;
  since the preamble strip that is the `## Classification` heading on its
  own, so the brief got "Doctor report: Classification" and the model
  dropped it (the 09-20 brief has no doctor line at all). Now: skip
  heading-only paragraphs, prefix the section, take the first bullet.
- **The brief cannot see the doctor's asks.** `_attention_ask_user` counts
  `OPEN:ask-user*` todos; the doctor's needs-a-human todos are
  `waiting-for:reto` (64e97785). The 09-20 brief said "two tasks waiting on
  your input" the morning the doctor had minted twelve. Ruling needed:
  fold `waiting-for:reto` into the brief's attention count, or leave the
  doctor's queue to `/asks`.
- **The brief re-diagnoses alerts the doctor already classified.** Its
  09-20 lane-skipping paragraph ("something structural broke in the
  scheduler … root-cause rather than retry") contradicts the doctor's
  same-day classification (fail-closed config gate, gr179498). With the
  health line fixed the brief at least carries the doctor's top
  classification; whether the alert lane should defer to the doctor's
  verdicts is a follow-up.
- **Three doctor asks of 2026-09-20 are wrong and should be closed** (prod
  write, Reto's call): td360163 (P0 "melchior job_claude_inproc dark, 0 log
  lines in 24 h" — worker_logs has 7,781 lines naming that pass in the
  same window; casts and two diagnoses ran through it); td360164 and
  td366642 ("deploy / confirm b58a18a0 on melchior, skip text still
  generic" — b58a18a0 is an ancestor of the deployed 08f79a3a and the
  newest skip event, and even al348959's own `detail`, already carry the
  leg-specific text). The doctor's "0 log lines" read suggests its pass
  filter misses `precis.workers.runner` lines that name the pass in the
  message; worth a look when the doctor prompt is next touched.

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
