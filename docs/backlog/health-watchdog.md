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
- **Alert-triage disposition pass** — dispose of aged `/alerts` backlog
  (the 36-day/95-alert rot class); open: fold into `health_digest` or a
  separate pass; how aggressive may auto-resolve be vs file-a-gripe.
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
- Alert-triage: in `health_digest` or separate; auto-resolve aggressiveness.
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
