# Presentation points — the recurring self-maintenance passes

A catalogue of the **periodic health/quality audits** this project runs on
itself — the "we don't forget to check X" list. Each is a cadence *nudge*
(cheap script that only says when it's DUE) plus a judgment *pass* (a session
that actually does the work and stamps a dated log, which resets the clock).
They surface together in `/whatneedsdoing`'s repo-hygiene block (step 3a).

This file is the index / talking-track; the runbook per row is the how-to. When
you add a new cadenced pass, add a row here and a bullet in
`.claude/commands/whatneedsdoing.md`.

## Why this pattern

The system generates two kinds of slow rot that no single ship catches:
structural drift (docs, migrations, indexes) and **quality drift you can't see
without looking** (are sessions wasting tokens? is prod thrashing the DB? can
agents still *find* the right skill?). A per-ship gate can't catch these — no
code changed — so they need a clock. The nudge is tier-1 (a regex on a log
date, zero model); the pass is tier-3 (judgment). Keeping the two separate is
what makes the cadence cheap enough to run on every `/whatneedsdoing` without
burning a model until something is actually due.

## The cadenced passes

| Pass | Cadence | What it audits | Nudge script | Runbook |
|------|---------|----------------|--------------|---------|
| **Memory reconsolidation** | ≤ 1×/day | Memory index integrity + landed-thread pruning; on DUE, the per-claim currency ledger (stale anchors, gone worktrees) | `scripts/memory-lint` | `memory_consolidation_log.md` |
| **Sibling-repo path check** | weekly (self-gated) | `~/work/...` repo paths memory cites still exist on disk | `scripts/memory-lint` (folds in) | `docs/runbooks/memory-sibling-repos.md` |
| **Token-review** | 7 days | Recent local session transcripts for repeated token-waste (context bloat, wrong-tier agents, un-`rtk`'d firehoses) | `scripts/token-review` | `docs/runbooks/token-review.md` |
| **DB-thrash review** | 14 days | Prod `pg_stat_*` — long queries, seq-scan-heavy tables, never-used indexes, dead-tuple bloat | `scripts/db-thrash-review` | `docs/runbooks/db-thrash-review.md` |
| **Skill-search discoverability** | 30 days | Whether agents' `search(kind='skill')` calls find the right skill; menu relevance + caller satisfaction → "better search bits" | `scripts/skill-search-review` | `docs/runbooks/skill-search-review.md` |
| **Nightly build** | 24 hours | LOCAL full-suite health — catches green-main breakage from upstream dependency drift the ship gate can't see | `scripts/nightly --check` | `.nightly-status.md` |

## Every-run structural scans (no cadence — cheap enough to run each time)

These run inline on every `/whatneedsdoing`; each flagged item is its own
worktree → ship, but the *check* is free.

- **Migration collisions** — `scripts/migration-check` (two worktrees about to
  collide on the same migration number).
- **Orphan design docs** — `scripts/docs-orphans` (a plan doc left behind after
  its feature shipped → `docs-triage`).
- **Code anchors** — `scripts/coderef check docs` (a doc cites a `file.py::Sym`
  that no longer resolves).
- **Backlog done-gunk** — `scripts/backlog-lint` (an OPEN-ITEMS entry whose
  title says DONE but still sits in the active list).

## The through-line (the actual presentation point)

Skill search was the newest addition (2026-07) and is the clearest example of
*why* these exist: it's the fallback agents reach for only when they **don't**
already know a skill's slug — the exact novel-need moment discoverability
matters — yet **nothing logged whether it worked**. Reconstructing 20 days of
searches from transcripts showed the matcher was doing atomic whole-query
substring matching, so natural-language queries silently found nothing. No bug
report would ever have surfaced that; only a periodic *look* did. Every row in
the table above is the same bet: a recurring, cheap look at a quality surface
that degrades invisibly between ships.
