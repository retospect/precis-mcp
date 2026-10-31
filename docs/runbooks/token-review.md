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
`~/.cache/precis/token-review.log` is >7 days old (or absent). Inside the
window it's quiet. Run the pass when DUE, then
`scripts/token-review --stamp "<window, sessions scanned, findings filed>"` —
that resets the clock.

## Remit boundary

This pass owns **harness and agent behaviour**: delegation tier, un-`rtk`'d
firehoses, context bloat, redundant calls. `surface-review` (14 d) owns the
precis MCP surface itself — verbs, error messages, renders, vocabulary,
capability gaps. `skill-search-review` (30 d) owns discovery ranking.
Cross-reference at the edges; don't duplicate a finding into two logs.

## The pass (keep it tight — a scan, not an audit)

Don't hand-parse transcripts — `scripts/mine-sessions/` is the committed
extraction layer, shared with the other two passes.
`scripts/mine-sessions/run.sh --since 7d` gives you `out/scoreboard.md` (per-
session token totals, cache-read ratio, tool-result byte shares, the Bash
detour census) and `out/cards/`, which is where the `detour_census`,
`render_obesity` and `read_then_unused` detectors already did the counting
this pass used to do by eye. Then read the **repeated** patterns off it —
one-off inefficiency isn't worth a finding.

The underlying transcripts live under `~/.claude/projects/*/*.jsonl` plus
`*/subagents/agent-*.jsonl` (one dir per worktree; the sidechains are 91% of
the files and were missed by earlier globs).

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
6. **MCP verb surface** — `/context` "MCP tools" should stay near 6k tokens;
   above ~7k, find which verb grew (`tests/test_mcp_slim_schema.py` caps the
   total). Then check the core set against use: per verb, how often each
   non-core key is passed (`scripts/prod-psql`, read-only, last 14 days on
   `tool_calls.input_keys`, keys not in `CORE_PARAMS` from
   `precis.runtime.dispatch`). A non-core key in most calls of a verb belongs
   in core; a core key almost nobody passes can move to `args=`. The ledger
   records keys after `args=` is flattened, so it cannot count deprecated
   top-level use (spec `mcp-verb-schema-diet`).

## Output

Each finding becomes durable work, not a transcript note (it must outlive
compaction): a concrete fix → a `docs/backlog/` item; a systemic-but-unscoped one →
a `gripe`. Then `scripts/token-review --stamp` one line summarizing the pass.

Method notes: sample the 6-8 largest sessions of the window (sidechains
included), and dedup cache-read tokens by message id. The recurring patterns
live as numbered rules in `docs/backlog/token-review-hook-gaps.md` (D `sed -n`
instead of Read, E tail-polling instead of `Monitor`, F a feature build with
zero delegation); a recurrence is evidence appended to that item, not a new
gripe. Re-reading governing docs after each auto-compact and fleet
coordinator/wakeup chatter are the other usual large shares.

## Log

The clock is `~/.cache/precis/token-review.log` (written by
`scripts/token-review --stamp "<note>"`); entries before 2026-10-30 are in
git history (`git log -p docs/runbooks/token-review.md`).
