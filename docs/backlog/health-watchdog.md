---
status: draft
title: Health watchdog — remaining phases 3-5 (brief lane, surface canaries, alert triage, autonomy rungs)
model: opus
---

# Health watchdog — Phases 3–5

§D (the liveness net) of `cluster-scheduling.md`. **Phases 0–2 SHIPPED**:
`src/precis/workers/health_digest.py` + the `health_digest` scheduler
cadence, the shared `src/precis/health_checks.py` module (Layer-1 outcome
SQL checks + derived Layer-2 registry×config×logs coherence), the nursery
`host-dark` critical (gr186752), the remediation router
(`_route_findings` — condition-fingerprinted, auto-closing gripes after
per-class self-heal budgets, flood-capped), and the external
dead-man's-switch (`docs/runbooks/dead-mans-switch.md`). Present-state:
the module docstring. Full original design (Layer-1 outcome tables, router
class matrix): git history of `docs/backlog/health-watchdog.md`.

Corrected evidence (2026-08-10) gr202275: the watchdog itself was healthy
(cadence firing hourly, router filing gripes) — "health_digest_router
never ran" was a verification against a nonexistent pass name. The real
defect: `_resolve_enabled_somewhere`'s structural default still counted
`enable_env` as intended-on post-§L, so 10 enable_env-only default-OFF
passes were flagged "intended-on but silent" every cycle (false
positives; fixed this ship). The discovery-group flags are Layer-1
checks, a separate bucket. Follow-up (2026-08-11): `_sync_alerts`'s
resolve sweep only covered groups present in the current eval's checks,
so a group going *all-quiet* (finding-only sources like
`_layer2_checks`) stranded its open alerts + marker gripes forever —
the 10 coherence alerts/gripes stayed open after the fix deployed;
fixed by sweeping open `watchdog:%` sources absent from the eval.

SLA framing (kept — it shapes everything): "never really urgent — just
don't let it linger and rot for days." Nursery owns real-time critical;
this is the slow-rot layer. Design laws that survive: all checks are
deterministic SQL (the digest must still send templated when the LLM/fleet
is down); file only what won't self-heal; every filed item is
condition-linked and auto-closes; alarm on backlog-not-draining, never on
mere quiet.

## Remaining scope

**Phase 3:**

- **Brief lane** — a one-line health lane folded into the morning brief
  cast (`reading/briefing_cast.py`); addition to the ops-channel digest,
  not a replacement.
- **Surface canaries** — surface-type outcomes (PCB/CAD/export builds):
  last-use-ok + dep-present first; weekly synthetic canaries (render a
  trivial PCB/CAD) as the later add. Never alarm on mere absence of use.
- **Alert-triage disposition pass** — **CODE SHIPPED d4b0d354 (2026-08-11)**:
  parts (A) true `COUNT(*) OVER ()` on the three capped detectors +
  `Finding.total` + "showing oldest 50 of 297" in the alert detail
  (`alerts.raise_alert` gained `extra_meta` to carry `total`); (B) the
  router now files ONE aggregate auto-closing marker gripe per non-draining
  nursery backlog category (`_NURSERY_BACKLOG_SOURCES`, budget 24h, flood-
  capped) — no `created_at` age-out. Still open: (C) the actual prod-ops
  triage of the 297+540 genuinely-broken todos (the deployed aggregate
  gripe is the hand-off surface for it), and the brief-lane / surface-
  canary bullets below. Original design (kept for the "why"):
  DESIGN DECIDED (2026-08-11, opus
  root-cause dossier). The original "age-out the aged `/alerts` rot"
  framing is **refuted and unsafe**: a prod re-query of the nursery
  predicates showed the open backlog (131, oldest 46d) is not stale-
  uncleared alerts but the *visible tip* of a genuinely-live, LIMIT-50-
  masked backlog — **297 live orphan conditions (50 alerted), 540 live
  stuck-doable (50 alerted)**; 100% of the surfaced 50 are still true
  right now. Every detector ends `ORDER BY r.ref_id LIMIT 50`
  (`nursery.py:_detect_orphans/_detect_stuck_doable/_detect_child_failed_parked`),
  a noise-cap copy-pasted onto backlog-style detectors, so the same
  oldest-by-`ref_id` 50 re-fire every pass. A `created_at`-only auto-
  resolve would mark still-broken todos "resolved," and nursery would
  re-open the same fingerprint next pass with a **reset `created_at`** —
  erasing the staleness signal while faking a drained backlog (masking).
  Nursery's `resolve_stale_alerts` (per-pass, per-source) already works
  (298 historical `orphan` resolves); the pile-up is the LIMIT cap +
  no escalation, not a broken resolve path. **Approved design (safe):**
  (A) each capped detector reports a true `COUNT(*)` sibling so the
  alert/gripe body reads "50 of 297, oldest 46d"; (B) widen
  `_route_findings`'s `watchdog:%` filter (`health_digest.py:~1349`) to
  cover `nursery:%` (or lift to a shared helper), filing ONE aggregate
  auto-closing marker-gripe per non-draining category via the existing
  `_file_router_gripe`/`_auto_close_marker_gripe` infra — a human hand-
  off, **never** an alert auto-resolve; (C) the 297+540 genuinely-broken
  todos are prod-ops disposition (substrate-2), filed separately, not a
  code fix. Anchors: `src/precis/alerts.py` (`raise_alert`,
  `resolve_stale_alerts`, `_flip_resolved`), `src/precis/workers/nursery.py`
  (detectors + `run_nursery_pass`), `src/precis/workers/health_digest.py`
  (`_sync_alerts`, `_route_findings`).
- Optional capped auto-restart-once for daemon-death findings.

**Phases 4–5 — the autonomy ladder** (Pillar 6 of `cluster-scheduling.md`;
runs on the container substrate; injection safety `gr179498` is a Rung-1
prerequisite):

| Rung | Behavior | Prod risk |
|---|---|---|
| 0 (today) | condition-linked gripe → human fixes | none |
| **1 — auto-draft, human-ship** (recommended start) | unattended reproduce (red test) → `coder` to green gate → `reviewer` sign-off → ready-to-`/go` branch, ping | none (no autonomous deploy) |
| 2 — auto-ship a whitelisted narrow class | config-flag flip, doc/typo, dep bump: ship + deploy unattended behind post-deploy verify + auto-rollback (re-run the very SQL check that found the problem) | bounded to the whitelist |
| 3 — widen the whitelist | each class earns Rung 2 | grows deliberately |

Hard limits (stated honestly): the fixer can't fix its own substrate
(fleet death / broken deploy / DB-down stay human — the dead-man's-switch
class); prod-state-dependent bugs won't reproduce in a sandbox, so
reproduce-first correctly refuses and escalates; per-gripe attempt cap +
global kill switch + bounded per-fix token budget.

## Explicitly NOT in scope

Real-time paging (nursery owns critical); aggressive autonomous
remediation (no auto-VACUUM/REINDEX, no flag flips); a new dashboard; new
schema; replacing nursery/`alert`/self-heal passes.

## Open questions

- `backlog_groom` on for watchdog gripes, or nudge-only? (Leaning
  nudge-only per the SLA.)
- Which classes ever earn Rung 2, and the bar to be added.
- ~~Alert-triage: in `health_digest` or separate; auto-resolve
  aggressiveness.~~ RESOLVED 2026-08-11 (see Phase 3 above): widen
  `health_digest`'s `_route_findings` to `nursery:%`; no auto-resolve
  (gripe hand-off only — age-out masks a live backlog).
- LLM phrasing of the digest (template-first is the shipped posture).
- Post-deploy verify + rollback mechanics for Rung 2.

## Residuals (from OPEN-ITEMS)

- Out-of-band DB-liveness monitor: the 2026-07-05 ~8 h prod outage ran
  unalerted because every alerting path is DB-backed — an external SELECT 1
  watcher on a different host (fixer host / laptop cron) → Discord on
  failure; a worker-log-volume trend alarm is a cheap second signal.
- Set PRECIS_OPS_ALERT_TARGET on system-profile workers — critical push is
  dark until then (worker-restart/dead-worker alerts land only in /alerts).
- Reto want: a periodic ops agent that auto-gathers status (services, APIs,
  db load, fs space, memory, temperatures, odd log entries; are queues
  working, are we ingesting/classifying — maybe a status kind,
  view='all relevant') and has an LLM judge reasonability; plus an
  are-we-working-on-the-right-things prioritization agent.
