# Runbook — memory sibling-repo path currency (weekly cadence)

A recurring, fully mechanical check: does every `~/work/<name>` /
`~/work/projects/code/<name>` sibling-repo path cited in memory still exist on
disk? Cadence is 7 days, enforced by `scripts/memory-lint` (surfaced in
`/whatneedsdoing`, folded into its normal `— memory —` line — no separate
invocation needed).

## Why this exists

`scripts/memory-lint --currency`'s per-claim ledger already verifies gone
git branches/worktrees and in-repo paths missing on `main` — but it
deliberately **skips** absolute external paths, on the reasoning that
"memories legitimately cite `~/work/cluster`" (a sibling ansible repo). That
carve-out is exactly what let a real staleness bug hide for a month: `~/work/
cluster` was retired 2026-07-19 (folded into this repo's `deploy/` tree), and
a dozen memory files kept citing it as if it were still live — including one
that misled a session into declaring a fix "unreachable from here" when the
actual file had moved in-repo (2026-07-24 incident).

Unlike `token-review`/`db-thrash-review`, this check needs no judgment pass —
`[[ -e <path> ]]` is fully deterministic — so the script does the check
itself when due and self-stamps the log below. No human step required.

## When

`scripts/memory-lint` prints `sibling-repo check: DUE` (and then runs it) when
the newest dated line in this file's `## Log` is >7 days old (or absent).
Inside the window it just reports the last result.

## What it checks

Every memory topic file (excluding `MEMORY.md` and
`memory_consolidation_log.md`, same exclusion as the rest of memory-lint) is
scanned for `~/work/<name>` and `~/work/projects/code/<name>` path mentions
(and their `/Users/<user>/work/...` absolute spelling). Each unique root is
existence-checked on disk. A finding is a **suspect**, not an auto-fix — the
path may be legitimately gone (repo retired/renamed, same as `~/work/cluster`)
or may be a typo; resolve it the same way as any other currency-ledger
finding (adjust the reference, or if the whole memory is now stale, fold it
into a durable doc and delete the memory).

## Log

**2026-07-24** — check added; `~/work/cluster` references already swept
(this session) across all memory files ahead of the first automated run.

**2026-08-01** — 1 stale path(s): catpath-dev-deploy.md cited `~/work/…/catpath` (gone). False alarm on content: the memory already pointed at the live `/Users/reto/catpath` and only *mentioned* the dead path in its "is GONE" note — which the `~/work/` scanner (this file, step 6) matched as a live ref. Fixed by dropping the literal dead-path string from the memory body; scanner only greps `~/work/…`, so the correct out-of-tree path is invisible to it and no longer trips. 

**2026-08-01** — ✓ clean (re-verified after the catpath fix above).

**2026-08-02** — ✓ clean

**2026-08-10** — ✓ clean
