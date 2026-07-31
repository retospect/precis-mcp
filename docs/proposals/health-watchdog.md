---
status: draft
title: Health watchdog — periodic liveness/freshness digest with routed remediation
model: opus
---

# Health watchdog — periodic liveness/freshness digest with routed remediation

## Motivation / why

precis does many independent things — ingest, chase, categorize, canonicalize
into taproot, dream, write the morning brief, build CAD/PCB. When one of these
silently stops, it can rot for **days to weeks** before anyone notices. This is
not a hypothetical: the 2026-07-26→30 worker-agent outage stalled all
agent-profile work for 4 days undetected, and a prod pulse probe on 2026-07-31
found `chunk_keywords` dead ~26 days, `dream_agent` stalled 2 days, and a
**36-day-old** nursery alert backlog (111 open, 95+ aged >7d).

The SLA here is explicit and forgiving: **"never really urgent — just don't let
it linger and rot for days."** That single fact shapes the whole design. We do
*not* need real-time paging (nursery already pushes genuinely-critical alerts).
We need a **periodic digest that reaches out**, catches slow rot, escalates as
things age, and — the load-bearing part — **routes each finding onto a standing
fix-path** so detection is not, once again, divorced from remediation.

### What already exists (so we don't rebuild it)

- **`nursery`** — SQL-only, per-minute, 13 detectors incl. worker liveness
  (`dead-worker`, `worker-restart`, `dispatch-stall`, `orphaned-coordinator`);
  raises deduped `kind='alert'` rows that auto-resolve when the condition
  clears; fires a one-shot Discord push on a *new* `critical` (if
  `PRECIS_OPS_ALERT_TARGET` is set — **may be unset/dark in prod**).
- **`/status?tab=health`** — a "Liveness" panel already computes freshness per
  pipeline stage (paper/chunk/embed/summarize/news/dream/brief) — but pull-only.
- **`alert` kind + `/alerts`** — the human queue; agents triage via `get`.
- **Self-healing**: sweeper, job-retry with orphan cap, quest-loop reconcile,
  fetch/chase exponential backoff, paper-hygiene stranded-fetch requeue.
- **Autonomous fix rail (code bugs)**: `backlog_groom` (default-OFF) reads
  gripes → mints `fix_gripe` jobs → planner dispatches a `coder` → PR.

**The gap is not detectors.** It is (1) an *outbound cadence* for the
slow/non-critical signal, (2) an *age/persistence escalation*, and (3) a
*remediation router* that drops each finding on the correct existing rail and
auto-closes it when the condition clears.

## In scope

### 1. Two monitoring layers

**Layer 1 — Outcomes (short, stable, hand-picked).** ~15 end-to-end outcomes
the factory exists to produce. This list tracks the *mission*, not the
implementation, so it barely changes as passes churn. Each is one SQL query on
a **consequence**, which is robust to the flag mess: "papers in for 48h, zero
topic-tags out" trips whether the cause is a dead daemon, an off flag, a broken
model, or a bad deploy — we check the output, not enumerate the causes.

Two **check-types**:
- **Freshness** — there is a natural beat; alarm on silence past the budget.
- **Surface** — on-demand tools (PCB/CAD/export) with no daily beat; alarm on
  *broken-since-last-use* or a vanished dependency, **never** on mere quiet.

Severity legend: **P** = page-worthy (breaks a user-facing product or the
fleet) · **D** = digest line · **S** = soft / FYI. Breadth of *coverage*,
graduated *loudness* — "lots watched" must not mean "lots screaming."

**A · Ingest & corpus** (freshness)

| Outcome | Budget | Sev | 07-31 |
|---|---|---|---|
| Papers ingesting (`kind=paper` newest) | >6h | D | ✅ |
| Chunks extracted for new papers | >6h | D | — |
| Chunks embedded (oldest `embedding IS NULL`) | >2h | D | ❓ |
| Chunks summarized (`chunk_summaries`) | >12h | S | — |
| Chunks keyworded (discovery layer) | >6h | D | 🔴 26d |
| Chunks classified (`role3` / `chunk_tags`) | >12h | D | ⚠️ |
| Papers topic-tagged (`topic:`) *if enabled* | >24h | S | ⚠️ |
| OA full-text landing (stubs gaining `pdf_sha256`) | >48h | S | ⚠️ 6d |
| Citations extracted (`kind=citation`) | >48h | S | ⚠️ 22h |
| Paper reconcile / dedup running | >36h | S | — |
| Corpus PDF ledger fresh (`corpus_reconcile`) | >2d | S | ⚠️ 6d |

**B · Knowledge graph** (freshness)

| Outcome | Budget | Sev | 07-31 |
|---|---|---|---|
| Findings → claims (newest taproot edge) | >6h | D | ✅ |
| Concepts / `represents`-links growing | soft | S | — |
| Structure / pathway graph updates | soft | S | ⚠️ 19h |

**C · Research & chase**

| Outcome | Budget | Sev | 07-31 |
|---|---|---|---|
| Chase advancing findings | >12h | D | — |
| Deep-research surface (perplexity / websearch) | last-use-ok | S · surface | — |

**D · Daily reading products** (freshness)

| Outcome | Budget | Sev | 07-31 |
|---|---|---|---|
| Morning brief cast | >26h | P | ✅ |
| Evening meditation cast | >26h | D | ✅ |
| Cast audio (mp3) produced | >26h | D | ⚠️ |
| Card-forge (morning cards) | >26h | D | — |
| Anki sync | >24h | S | ⚠️ 7h |
| News flowing (`kind=news`) | >8h | D | ✅ |

**E · Autonomy & agents**

| Outcome | Budget | Sev | 07-31 |
|---|---|---|---|
| Dreams running | >6h | D | 🔴 2d |
| Quests advancing (active loop minting ticks) | >loop period | D | — |
| Agent jobs completing (not all-failing) | >6h | P | — |
| Planner / quest not spinning | nursery `critical` | P | — |

**F · Build surfaces** (surface — on-demand; alarm on broken-since-last-use, never on quiet)

| Outcome | Signal | Sev |
|---|---|---|
| Draft export (resolve / latex / docx) | last export ok + deps | D |
| PCB build | last build ok + deps present | D |
| CAD build | last build ok + deps present | D |
| DFT / structure-relax compute | last job ok + GPU slot present | S |

**G · Delivery & integration**

| Outcome | Signal | Sev |
|---|---|---|
| asa / Discord bridge (messages delivered) | delivery-fail | P |
| Web reader reachable (`melchior:8000`) | liveness probe | P |
| Drive publishing (casts land) | last-use-ok | S |
| Email IMAP browse (`mail_poll`) | surface (off today) | S |

**H · Platform / infra** (liveness)

| Outcome | Signal | Sev | 07-31 |
|---|---|---|---|
| Hosts alive (heartbeat) | >15m | P | ✅ |
| DB health (bloat / vacuum age / conns / long queries) | thresholds | D | — |
| Disk / corpus storage headroom | <threshold | P | — |
| Inference services up (embedder / llama-swap / marker / ollama) | liveness | D | — |
| LLM router healthy (auth / budget / cloud reachable) | `quota_check` | P | — |

**I · Meta / self**

| Outcome | Signal | Sev | 07-31 |
|---|---|---|---|
| Alert backlog not rotting (open >7d) | >0 sustained | D | 🔴 95 |
| Gripe backlog not rotting | >N sustained | S | — |
| **Watchdog itself beating** | the digest (dead-man's-switch) | P | — |

> The delivery-path rows (asa bridge, web reader) and the "watchdog itself
> beating" row create a **bootstrap**: the digest reaches you *through* asa, so
> asa being down can't be reported by the digest. That gap is exactly what the
> external dead-man's-switch (below) covers — the one signal that survives a
> total fleet outage.

Budgets are seeded **empirically** from the observed cadence (the pulse probe),
not guessed — the watchdog's own bootstrap method (see Acceptance).

**Layer 2 — Component liveness (derived, self-updating, never hand-written).**
When a Layer-1 outcome trips, the nudge names the likely culprit by joining the
system's *own* declarations: `workers/registry.py` (`ServiceSpec` — CI already
fails if a wired pass has no spec, so this list is guaranteed complete) ×
`service_config` (intended prio/on-off) × `worker_logs` (observed). This yields
the **config-coherence check** for free: "prio=5 row (intended ON) but zero log
rows in 24h", or "registered + default-on but no host running it" — the flag
mess made legible with no parallel list to maintain. DB health is likewise
introspected from the catalog (`pg_stat_user_indexes`, vacuum age, bloat), not
a hand list of expected indexes.

### 2. The remediation router (the watchdog is a router, not a fixer)

Each finding is classified and dropped on a standing rail:

| Class | Example | Rail | Touch |
|---|---|---|---|
| Transient | missed cycle, expired lease | existing self-heal (sweeper/retry/backoff/reconcile); **suppress**, escalate only past the self-heal's own budget | zero |
| Daemon death | `dream_agent` | nursery critical-push → `cluster-admin` runbook restart; capped auto-restart-once optional later | nudge → act |
| Config/flag drift | `chunk_keywords` off? | condition-linked **gripe with the exact toggle** → `/whatneedsdoing`; cannot auto-flip (intent) | you decide |
| Code bug | pass throwing on input | gripe → `backlog_groom` → `fix_gripe` → `coder` PR → you `/go` | auto-attempt → you ship |
| Infra / DB | missing index, bloat | nudge with the exact DDL (`CREATE INDEX CONCURRENTLY …`) | you run |
| Response-loop rot | 95 aged alerts | the digest + escalation itself; + an alert-triage disposition pass | the watchdog |

### 2b. Automatic fix loop — the autonomy ladder

The router can climb from *nudge* to *auto-fix* one rung at a time, each rung
earning trust before the next. The safety spine is identical at every rung:

- **Reproduce-first.** No fix without a red repro test — a change is only ever
  merged because it turned a *known-red* test green (`test-author` writes it).
- **The deterministic `scripts/ship` gate** (ruff / mypy / pytest) is the
  arbiter, plus an adversarial `reviewer` sign-off.
- **Post-deploy outcome verification with auto-rollback** (Rung 2+): the
  watchdog re-runs *the very SQL check that found the problem*; if the outcome
  doesn't go fresh, roll back and escalate. **The watchdog both finds and
  confirms the fix — a closed loop.**

| Rung | Behavior | Prod risk |
|---|---|---|
| **0** (today) | file a condition-linked gripe → human fixes | none |
| **1 — auto-draft, human-ship** *(recommended start)* | unattended worktree/sandbox reproduces → `coder`(-chain) fixes to green gate → `reviewer` signs off → **stops and pings you with a ready-to-`/go` branch** | none (no autonomous deploy) |
| **2 — auto-ship a whitelisted narrow class** | for classes proven safe (config-flag flip, doc/typo, dep bump): ship + deploy unattended, gated by post-deploy verify + auto-rollback | bounded to the whitelist |
| **3 — widen the whitelist** | each class earns Rung 2 as it proves out, always behind the full spine | grows deliberately |

**Rung 1 is the honest place to start:** ~80% of the value (the fix is written
and proven while you sleep) at ~0% autonomous-deploy risk, and it's a thin
extension of pieces already present — `backlog_groom` → `fix_gripe` → `coder` →
the ship gate. "Set aside a spot with a sandbox that gets stuff done and
deploys it" = a dedicated worktree/container (the proposed
`sandbox-run-substrate`) running Rung-1 chains unattended and handing you a
branch. Deploy stays your keystroke until a class earns Rung 2.

**Hard limits, stated honestly:**
- **The fixer can't fix its own substrate.** It runs on the agent profile
  (melchior), so fleet death, a broken deploy, or DB-down are inherently
  human/external — the loop must recognize its own blind spot and escalate, not
  spin. (This is the exact class the external dead-man's-switch guards.)
- **Prod-state-dependent bugs won't reproduce in a sandbox** (a dev DB won't
  recreate "`chunk_keywords` stopped populating"), so reproduce-first will
  *correctly refuse* to auto-fix them and escalate. The auto-fixable set is
  smaller than it looks — that's the safety property, not a shortfall.
- **Thrash guard.** A per-gripe fix-attempt cap (like the orphan-retry cap) + a
  global kill switch + a bounded per-fix token budget.

### 3. Two anti-rot rules (so the router does not become a second pile-up)

1. **File only what won't self-heal.** A finding becomes a gripe only after it
   outlives the relevant auto-recovery budget. (Otherwise we'd have filed 95
   gripes for the 95 alerts.)
2. **Every filed item is condition-linked and auto-closes when the outcome goes
   fresh again** — exactly how nursery alerts auto-resolve. *No finding
   outlives its cause.* The 36-day alerts rotted precisely because their
   condition never cleared **and** nothing routed them to a fixer.

### 4. Delivery

- **Standalone daily digest** pushed to the ops Discord channel (same
  `pg_notify('precis.messages')` → asa path the brief uses). **Pushes even when
  all-green** — a terse "✅ all green" is the dead-man's-switch that proves the
  watchdog itself is alive (a silent watchdog is indistinguishable from a dead
  one). Louder / pinned when something is aging.
- **A one-line health lane folded into the morning brief cast** — the friendly
  surface, riding what you already consume. Addition, not the only path.

### 5. SQL-first discipline

All checks are **deterministic SQL**, like nursery — the watchdog must not
depend on the subsystems it monitors (the agent fleet dying is the thing you
most need to hear about). An LLM may *phrase* the narrative, but the digest
must still send, with a plain templated body, when the LLM/fleet is down. (The
pulse probe itself demonstrated why: a one-shot LLM pass false-flagged
`classify` and mislabeled several cadence periods — fine for scouting, unfit to
page you.)

## Explicitly NOT in scope

- **Real-time paging / new critical-alert channel.** nursery already owns
  critical; this is the slow-rot layer.
- **Aggressive autonomous remediation.** No auto-`VACUUM`/`REINDEX`, no
  auto-flipping operator flags, no unattended shipping of code fixes. Detect +
  nudge + route; the human keeps the veto (matches the "never urgent" SLA).
- **A new dashboard.** `/status` already has the health surface; a small tab
  addition at most. The point is *outbound*, not another page to pull.
- **Replacing nursery, `alert`, or the self-heal passes.** This composes with
  them; it does not reimplement liveness detection.
- **New schema.** Reuse `gripe` / `message` / `alert` + an `app_state` marker
  for last-run/escalation state. No migration unless a check genuinely needs one.

## Acceptance criteria

1. A digest message reaches the ops channel **on a daily cadence even when
   every outcome is green** (dead-man's-switch), enumerating per-outcome status.
2. Every Layer-1 outcome has a deterministic SQL check + an empirically-seeded
   budget, and trips within **one cadence** of the outcome going stale.
3. A tripped, non-self-healing, *persistent* condition files **exactly one**
   condition-fingerprinted gripe; a repeat sighting does **not** duplicate; the
   gripe **auto-closes** when the outcome returns fresh.
4. Self-healing classes are **suppressed** (no gripe, no page) until they
   outlive their recovery budget.
5. The digest is produced by SQL and **still sends** with a templated body when
   the LLM/agent fleet is unavailable.
6. Layer-2 component liveness is **derived** from `registry.py` +
   `service_config` + `worker_logs` — adding a new pass requires **zero** edits
   to the watchdog (verified by a test that mints a spec and sees it appear).
7. Surface-type outcomes (PCB/CAD/export) alarm on broken-since-last-use or a
   missing dep, and **never** on mere absence of recent use.
8. Age-escalation is observable: a condition open >Nd is visibly ranked/marked
   louder than a fresh one in the digest.

## Target + blast radius

- **New worker pass** `health_digest` in `src/precis/workers/` + a
  `ServiceSpec` row in `registry.py` (system profile; single-runner via
  advisory lock; ~hourly evaluation, daily push, or push-on-change + daily
  heartbeat — see open questions).
- **Reads**: `refs`, `chunks`, `worker_logs`, `host_heartbeat`,
  `service_config`, `links`, `ref_tags`, pg catalog views.
- **Writes**: `kind='gripe'` (condition-linked, auto-closing), `kind='message'`
  (Discord push via `pg_notify`), an `app_state` marker for escalation state.
- **Delivery**: asa_bot `precis.messages` channel; morning-brief lane in
  `reading/briefing_cast.py`.
- **Possibly**: a small `/status` tab surfacing the same digest; an
  alert-triage disposition pass (Phase 3).
- **Docs**: `state-map.md` (Review-tiers / Workers sections), a `precis-*-help`
  skill, `OPEN-ITEMS.md`.

### Suggested phasing

- **Phase 0 (quick wins, hours):** verify/set `PRECIS_OPS_ALERT_TARGET`
  (a dark push target may already explain much); decide whether to enable
  `backlog_groom` so the code-fix rail is live.
- **Phase 1:** `health_digest` pass — Layer-1 SQL checks + Layer-2 coherence,
  daily green-heartbeat push, age-escalation. Deterministic, no LLM required.
- **Phase 2:** the remediation router — condition-linked gripe filing with
  auto-close, class-based routing + self-heal-aware suppression.
- **Phase 3:** brief lane; surface canaries; alert-triage disposition pass;
  optional capped auto-restart; external dead-man's-switch.
- **Phase 4 (autonomy Rung 1):** auto-draft-human-ship — unattended
  reproduce → fix → gate → review → hand you a branch. Depends on
  `sandbox-run-substrate`.
- **Phase 5 (autonomy Rung 2):** auto-ship + deploy for a whitelisted narrow
  class, behind post-deploy verify + auto-rollback.

## Open questions / decisions log

- **backlog_groom on?** Turn on the autonomous code-fix rail for watchdog
  gripes, or keep it nudge-only? (Autonomy appetite; leaning nudge-only per the
  SLA, with the rail as an opt-in.)
- **Budgets — where?** Code constants (like nursery's thresholds) vs a
  `service_config`-style table for live tuning without redeploy?
- **Cadence shape.** Daily push at a fixed time, vs push-on-change + a daily
  green heartbeat? (The heartbeat is non-negotiable; the question is the
  between-beats behavior.)
- **Surface checks.** last-use-ok + dep-present first, with weekly synthetic
  canaries (render a trivial PCB/CAD) as a later add — confirm ordering.
- **External dead-man's-switch.** In scope now or Phase 3? A cron-ping /
  healthchecks.io outside precis is the only signal that survives a *total*
  fleet outage (a dead precis can't report itself). Which provider.
- **Alert-triage pass.** Fold disposition of aged alerts into `health_digest`,
  or a separate pass? And how aggressive may auto-resolve be vs file-a-gripe?
- **LLM phrasing in v1?** Pure template first (robust), add friendly phrasing
  once the SQL spine is trusted — or phrase from day one with template fallback?
- **Prune / tune the Layer-1 list.** ~40 outcomes now (groups A–I). Confirm the
  severity tiers (which are genuinely page-worthy vs digest-line vs soft), and
  whether any group is over-reach (e.g. is DFT-compute or email-browse worth
  watching at all).
- **Which classes ever earn Rung 2 (unattended deploy)?** The initial
  whitelist — config-flag flips, doc/typo, dep bumps — and the bar a class must
  clear to be added. Everything else stays Rung 1 (auto-draft, human-ship).
- **`sandbox-run-substrate` dependency.** Rung 1+ needs the set-aside
  worktree/container substrate; confirm that proposal is the intended host and
  sequence it before Phase 4.
- **Post-deploy verify + rollback mechanics.** How the watchdog re-checks a
  specific outcome after a deploy, and what "rollback" means operationally
  (revert-commit + redeploy vs a pinned previous release).
