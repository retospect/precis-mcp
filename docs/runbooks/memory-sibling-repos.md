# Runbook — memory sibling-repo path currency (weekly cadence)

A recurring, fully mechanical check: does every `~/work/<name>` / <!-- home-path: allow — names the shape the sibling-path check scans -->
`~/work/projects/code/<name>` sibling-repo path cited in memory still exist on <!-- home-path: allow — names the shape the sibling-path check scans -->
disk? Cadence is 7 days, enforced by `scripts/memory-lint` (surfaced in
`/whatneedsdoing`, folded into its normal `— memory —` line — no separate
invocation needed).

## Why this exists

`scripts/memory-lint --currency`'s per-claim ledger already verifies gone
git branches/worktrees and in-repo paths missing on `main` — but it
deliberately **skips** absolute external paths, on the reasoning that
"memories legitimately cite `~/work/cluster`" (a sibling ansible repo). That <!-- home-path: allow — names the shape the sibling-path check scans -->
carve-out is exactly what let a real staleness bug hide for a month: `~/work/ <!-- home-path: allow — names the shape the sibling-path check scans -->
cluster` was retired 2026-07-19 (folded into this repo's `deploy/` tree), and
a dozen memory files kept citing it as if it were still live — including one
that misled a session into declaring a fix "unreachable from here" when the
actual file had moved in-repo (2026-07-24 incident).

Unlike `token-review`/`db-thrash-review`, this check needs no judgment pass —
`[[ -e <path> ]]` is fully deterministic — so the script does the check
itself when due and self-stamps its log (`~/.cache/precis/memory-sibling-repos.log`,
outside the repo). No human step required.

## When

`scripts/memory-lint` prints `sibling-repo check: DUE` (and then runs it) when
the newest dated line in `~/.cache/precis/memory-sibling-repos.log` is >7 days
old, or has none (fresh machine: one extra run). Inside the window it just
reports the last result. Entries before 2026-10-30 are in git history
(`git log -p docs/runbooks/memory-sibling-repos.md`).

The log is untracked on purpose: the check used to append here, which dirtied
every checkout it ran in, including the primary `main` checkout (blocking
ship's primary fast-forward). A new result is a line in the cache file; read
it with `tail ~/.cache/precis/memory-sibling-repos.log`.

## What it checks

Every memory topic file (excluding `MEMORY.md` and
`memory_consolidation_log.md`, same exclusion as the rest of memory-lint) is
scanned for `~/work/<name>` and `~/work/projects/code/<name>` path mentions <!-- home-path: allow — names the shape the sibling-path check scans -->
(and their `/Users/<user>/work/...` absolute spelling), plus `../<name>`
checkout-relative references (resolved against the main checkout — the
spelling new references use since 2026-10-22). Each unique root is
existence-checked on disk. A finding is a **suspect**, not an auto-fix — the
path may be legitimately gone (repo retired/renamed, same as `~/work/cluster`) <!-- home-path: allow — names the shape the sibling-path check scans -->
or may be a typo; resolve it the same way as any other currency-ledger
finding (adjust the reference, or if the whole memory is now stale, fold it
into a durable doc and delete the memory).

A memory that only *mentions* a retired path in a "this is gone" note still
trips the scanner (it matches the literal): reword the memory to drop the path
string rather than treating the finding as real.

