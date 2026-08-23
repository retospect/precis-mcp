# Runbook — token-review (session-tightness cadence)

A recurring, **local** pass that asks one question: *where are these Claude
sessions wasting tokens, and how do we tighten them?* Cadence is 7 days,
enforced advisory-style by `scripts/token-review` (surfaced in
`/whatneedsdoing`, next to `memory-lint`). It is **not** a cloud cron — the data
is the local session transcripts, which only this machine can see.

Tiering (per CLAUDE.md "three tiers"): the *cadence check* is a script (tier 1,
zero model); the *review itself* is a judgment session (tier 3) — spotting
waste patterns in transcripts needs a capable model reading them, not a regex.
The script only tells you **when** it's due; you run the pass.

## When

`scripts/token-review` prints `token-review: DUE` when the newest dated line in
this file's `## Log` is >7 days old (or absent). Inside the window it's quiet.
Run the pass when DUE, then append a dated line (below) — that resets the clock.

## The pass (keep it tight — a scan, not an audit)

Read a handful of recent, large local session transcripts and look for the
**repeated** waste patterns — one-off inefficiency isn't worth a finding. The
transcripts live under
`~/.claude/projects/-Users-reto-precis-mcp*/*.jsonl` (one
dir per worktree; newest/biggest first).

What "waste" means here, in rough priority:

1. **Context bloat** — sessions that ran long enough to auto-compact, or tripped
   the `session-size-nudge` hook (`PRECIS_SESSION_NUDGE_MB`). Ask *why* they got
   big: re-reading the same file, dumping whole files when a range would do,
   verbose tool output never compacted.
2. **Wrong-tier agents** — Opus spawned (bare `general-purpose`, or the main
   loop doing it inline) for mechanical work that a haiku agent
   (`navigator` / `extract` / `test-runner` / `tidy`) or a plain script should
   have done. This is the payoff of the cheap-agent defs — check they're
   actually being reached for.
3. **Un-`rtk`'d firehoses** — verbose commands (`git log`, `psql`, `rg`/`find`
   over the tree) run raw instead of `rtk <cmd>`, spilling pages into context.
4. **Redundant tool calls** — the same read/search issued repeatedly, probes
   that a single call would answer, serial calls that could have been batched.
5. **Prompt/skill friction** — a skill or CLAUDE.md instruction that's routinely
   misread and forces a correction round-trip (overlaps the LLM-confusion mine
   in `/whatneedsdoing` step 4 — cross-reference, don't duplicate).

## Output

Each finding becomes durable work, not a transcript note (it must outlive
compaction): a concrete fix → a `docs/backlog/` item; a systemic-but-unscoped one →
a `gripe`. Then append **one** dated line here summarizing the pass — newest
first, so the script reads the top:

## Log

- **2026-08-23** — sampled 6 largest 08-15–08-23 sessions (5.3–19.5 MB, all
  in the primary checkout). Rule D's `echo "==="` half is fixed (0/6 files,
  vs. 73% of one session's Bash calls pre-fix); `sed -n` half is not (2–76
  calls/file still). Two new patterns filed as Rules E/F in
  `token-review-hook-gaps`: (e) raw `tail .../tasks/<id>.output` polling for
  background-job status instead of the `Monitor` tool (~111 polls vs. 9
  `Monitor` calls across the sample, 3/6 files used `Monitor` zero times);
  (f) one session (`b30f9d07`, web-basic-auth feature build) ran almost fully
  un-delegated — 366 raw Bash, 104 Edit, 0 `coder` dispatches for standard
  multi-file feature work, plus 34 raw `ssh melchior` calls with 0
  cluster-ops delegation (Rule B confirmed still ignored, at unusually high
  volume) — flagged as a candidate Edit-count nudge, held for recurrence
  before committing to a hook. Rules A–C: cluster-ops delegation and
  compact-thrash re-reads both showed up mixed (2/6 files clean, others still
  re-reading governing docs 4–10× across compacts) — no regression, no new
  fix warranted this pass.
- **2026-08-15** — sampled 6 largest 08-08–08-15 sessions (6–15 MB). New
  pattern (the 07-29/08-07 hook fixes are holding — cluster-ops delegation and
  coderef-nudge both confirmed working): `sed -n '<range>p' <local-file>`
  instead of the Read tool, and `echo "=== label ==="` narration wrapping
  compound Bash probes — both explicitly named in CLAUDE.md but outside
  `bash-reflex-nudge.py`'s Rule A–C coverage and outside rtk's rewrite list
  (`sed` isn't a known command). Concentrated in primary-checkout orchestration
  sessions: worst session had `echo "==="` on 308/420 Bash calls (73%) + 36
  `sed -n` calls; another had 63 `sed -n` calls ≈43K tokens of tool_result.
  Folded into the existing `token-review-hook-gaps` backlog item as Rule D.
- **2026-08-07** — sampled the 6 largest 07-30–08-05 sessions (7–18 MB;
  transcripts live under `~/.claude/projects/-Users-reto-precis-mcp*/` —
  the stale path in "The pass" above corrected this pass). Verdict: the 07-29
  `bash-reflex-nudge` hook is NOT moving behavior — Rule A never fires on the
  dominant multi-pattern exploratory greps (24–75/session vs 0–4 navigator
  uses) and Rule B nudges per-call with no escalation (55 inline ssh/psql in
  one session, nudged every time, proceeded every time). Plus a new pattern:
  marathon multi-day sessions compact-thrash (5–7 auto-compacts) and re-Read
  the same governing design doc in full after each compact. Both filed as one
  backlog item (hook coverage/escalation + PreCompact state-note nudge).
  rtk confirmed working; no skill-friction correction loops found.
- **2026-07-29** — first real pass (sampled 6 large 07-26–07-29 sessions across
  worktrees). Two token-waste patterns found, **both fixed same session** by the
  `bash-reflex-nudge` PreToolUse hook (`873f7ce2`): (1) `coderef-nudge` was
  scoped to the native `Grep` tool only, so it never fired on Bash-invoked
  `rg`/`grep` — ~100% of real code-search traffic (168 raw calls vs. 0
  `search_code`/`navigator` across the sample); the hook's Rule A now nudges
  bare-identifier Bash greps toward `coderef`/`search_code`. (2) cluster
  ops/log/psql diagnosis (~30 raw `ssh`/`prod-psql` calls, up to 49 KB each) ran
  inline on the Opus main loop instead of `cluster-ops`/`cluster-admin` during a
  23h classify-throughput investigation (the single largest token-cost pattern);
  the hook's Rule B now nudges `ssh <node>` / `scripts/prod-psql` toward those
  agents.
- **2026-07-18** — cadence established (this runbook + `scripts/token-review`).
  Baseline pass deferred to the first DUE firing; no findings yet.
