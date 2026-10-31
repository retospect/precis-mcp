# Runbook — surface-review (MCP surface cadence)

A recurring pass that asks one question: *where did the precis MCP surface
confuse an agent, cost more tokens than it should, or turn out to be missing
a capability?* Cadence is 14 days, enforced advisory-style by
`scripts/surface-review` (surfaced in `/whatneedsdoing`, next to
`token-review` / `skill-search-review` / `db-thrash-review`). The runnable
pass is `/surface-review`.

Findings can be anything at any level: a misleading error message, a skill
that teaches a dead grammar, an argument name that fights intuition, a render
nobody reads in full, a verb that does not exist so every agent detours to
SQL.

Tiering (per CLAUDE.md): the *cadence check* is a script (tier 1, zero
model); the *extraction, statistics and detectors* are scripts
(`scripts/mine-sessions/`, tier 1); the *judging* is a model reading evidence
cards (tier 2 — sonnet `forensics` agents, fanned out one per detector
family). Only the scoreboard and the ranked digests reach a main loop.

## Remit boundary

Three passes mine the same transcripts. They share `scripts/mine-sessions/`
and divide the subject:

| Pass | Cadence | Owns |
|---|---|---|
| **surface-review** (this) | 14 d | The precis MCP surface: verbs, error messages, renders, vocabulary, capability gaps. |
| `token-review` | 7 d | Harness and agent behaviour: delegation tier, un-`rtk`'d firehoses, context bloat, redundant calls. |
| `skill-search-review` | 30 d | Skill discovery: does `search(kind='skill')` return the right menu, and does the matcher rank it right. |

Overlap is expected at the edges (a skill that teaches a broken call is both
a discovery problem and a surface problem). Cross-reference; do not duplicate
a finding across two logs.

## When

`scripts/surface-review` prints `surface-review: DUE` when the newest dated
line in `~/.cache/precis/surface-review.log` is >14 days old (or absent).
Inside the window it is quiet. Run the pass when DUE, then stamp it (step 6)
— that resets the clock.

## Calibration — read this before the error table

The measured error rate on this surface is **0.8%** (609 errors in 73,472
verb calls over 10 days, `tool_calls`). **Errors are not where the waste
is.** The dominant signal is successful-but-clumsy: fat renders, N calls
where one would do, detours to raw SQL because no verb exists. A pass that
opens with the error histogram will find a rounding error and call the
surface healthy.

Read the scoreboard's byte columns and `retry_to_success` first. Errors
matter for what they *teach* (a badly worded error is one retried three
times), not for their rate.

## Corpora, and what each is good for

| Corpus | Window | Good for | Blind to |
|---|---|---|---|
| Local `.jsonl` (`~/.claude/projects/`) | disk-bound | Everything with a payload: full tool stream, exact per-turn tokens, non-precis detours, **user corrections**. | Anything that happened on the cluster. |
| `tool_calls` (migration 0133) | 30 d | Fleet-wide `(verb, kind, error_type)` rates, latency, `input_keys` shapes, profile split. Covers MCP server + CLI + ticks. | Causes. It carries **no payload, ever** — `input_keys` is argument *names* only. |
| `llm_call_log` ⋈ `llm_blob` | 90 d | Prompt/response text, cost, `data_parsed` as a prompt-quality signal. | Tool calls. |
| `refs.meta->>'transcript'` (`kind='job'`) | 30 d | Server-side tool streams. | Almost everything now — see below. |

**The prod transcript corpus has collapsed.** A 10-day window held 2
`plan_tick`, 51 `quest_tick` (24 kB total) and 30 `doctor_tick`. The
`[error:…]`-regex-over-transcripts method that `/whatneedsdoing` step 6 grew
up on now mines near-nothing; `tool_calls` replaced it for rates and the
local corpus replaced it for causes. Keep `--jobs` in the run because the
window may predate the collapse, but do not expect it to carry the pass.

Rank slow verb/kind pairs with `precis.health_checks.tool_latency(conn,
window_s=…)`: the same p50/p95 rollup precis-status shows and the
`tool_latency` health-digest check alerts on, so a ranking never
disagrees with the alert.

## The pass

`/surface-review [window]` runs this. By hand:

0. **Read the known set first.** Open gripes —
   `search(kind='gripe', tags=['STATUS:open'])`, with **no `q=`**: adding one
   turns a complete enumeration into a ranked filter and the count silently
   drops. Plus the surface-family items in `docs/backlog/INDEX.md`
   (`mcp-surface-economy`, `capability-discovery-on-a-sprawling-surface`,
   `vocab-compaction`, `token-review-hook-gaps`, `llm-prompt-surface-audit`,
   `mcp-verb-schema-diet.md`, `tick-tool-lists-and-discovery-reflex`). Every
   candidate gets marked known/new against this set. The territory is already
   well-populated; the pass's job is to put *measurements* on those items,
   not to mint more of them.
1. **Mine.** `scripts/mine-sessions/run.sh --since <window> --prod --random 36`.
   Scripts only, zero model. Produces `out/scoreboard.{md,json}`,
   `out/candidates.json`, `out/cards/**`.

   **Size the random arm at 30–40, not the default 10.** Pass #1 ran 12 and
   the arm still produced the pass's most valuable finding — but it could
   only call it "plausible, n=2, sanity-check on a bigger pull before
   filing". The cards are cheap to render and a thin random arm is the one
   place this pass systematically under-reads.
2. **Read the scoreboard** on the main loop. One screen. Note what moved
   since the last pass — `out/scoreboard.json` carries a `schema_version` so
   the numbers are comparable.
3. **Fan out.** One `forensics` agent per detector directory, each reading
   only its own cards plus the owning handler/skill file — **plus one agent
   on `out/cards/random/`** asked the open question "what confused this
   agent?" with no detector attached. That arm is how a pass finds shapes the
   catalogue does not encode; without it the detectors only confirm
   themselves. Raw transcripts never reach the main loop.
   Each agent returns findings in the `forensics` shape
   (`signature — frequency — likely cause — next step`) classified into a fix
   class: **skill edit · error-message fix · MCP capability · render diet ·
   task-template fix · harness-hook fix · doc fix**.
4. **Gate before filing.** Both traps are real and both have burned a pass
   (gr51426 hit both):
   - **Demand a real failure.** Only count a match inside a `tool_result`
     with `"is_error":true` tied to a `tool_use.id` — not narration, not a
     prior assistant message, and most insidiously not a gripe or backlog
     body *quoting* the error string, read back into a fresh tick.
   - **Prove it is unfixed.** Pull the real occurrence timestamps
     (`ORDER BY ts DESC`). If they cluster in the past, cross-reference the
     fix's deploy sha (`git log -S'<callsite>'`, then
     `git merge-base --is-ancestor <fix_sha> <deployed_sha>`). If every real
     occurrence predates the fix's deploy, it is **already fixed** — a stale
     artifact, not new work. Do not file it.
5. **File.** Default is to **append measured evidence to the existing item**
   — a section on the backlog file, or a comment via
   `put(kind='gripe', id=N, text=…)` (there is no `edit(kind='gripe')`). Mint
   a new gripe, through `gripe-filer` so the dedup step runs, only for a
   confirmed-live cause that nothing covers. Fix the cheap ones in-session:
   error wording, a skill's vocabulary, a stale doc.
6. **Stamp.** `scripts/surface-review --stamp "<window, corpus size, headline
   numbers, what shipped, what was filed; whether PRECIS_FRICTION_REFLECT
   stays off>"`.

Miner caveats, to discount before trusting a card:

- The ledger carries no payload, so fleet byte cost is a PROXY (extrapolated
  from the local mean result size), never a measurement.
- Adjacency detectors (`narrow-after-fat`, reformulation) must not run over
  the ledger's day-bucket pseudo-session, and need same-verb/same-id to count
  as narrowing. `read_then_unused` saturates near 100% (it emits
  `PROXY-SATURATED`).
- The asa bots' 30 s `get(kind='gripe', id='/open')` liveness ping is dropped
  by the miner but still counted by other ledger consumers (gr492453).
- Known detector false positives: `skill_taught_wrong` counts a legitimate
  NotFound and blames the last-read skill; `session_digest` drops assistant
  text; `exec_class zero` counts empty-capture rows.

## Levers

- **Error messages** (`src/precis/errors.py` subclasses raised by handlers) —
  the `retry_to_success` metric names the ones that fail to teach. An error
  that says what was wrong but not what to do next gets retried.
- **Skills** (`src/precis/data/skills/*.md`) — `skill_taught_wrong` names the
  file. Vocabulary that must surface goes in an `## H2`; vocabulary that must
  *rank top* goes in the identity (slug / `title:` / `# H1`).
- **Renders** — `render_obesity` plus narrow-after-fat says which
  `(verb, kind, view)` hands back more than the caller reads. The fix is
  usually a default `view`, not a new one.
- **Capability** — `detour_census` ranks missing verbs by demand. A recurring
  psql detour is a feature request with usage data attached; see
  `docs/backlog/mcp-surface-economy.md` §"MCP aggregate surface gaps".
- **Argument names** — `vocab_near_miss` on `input_keys`. Feeds
  `docs/backlog/vocab-compaction.md`.

## The strongest lever we are not pulling

`src/precis/utils/friction_reflect.py` already implements end-of-run
self-reporting for exactly the soft friction that dominates here — "no verb
for what I wanted, N calls where one should have worked, a result shape that
forced a re-query". It is **default-OFF** behind `PRECIS_FRICTION_REFLECT`,
blocked on a grouping/dedup lane so raw wishes do not pile up untriaged
(`docs/backlog/friction-reflection-enable.md`).

Turning it on converts this retrospective into a continuous feed. Until then
this pass is the only thing looking. Re-read that item each pass and say in
the stamp note whether it is still the right call to leave it off.

## Log

The clock is `~/.cache/precis/surface-review.log` (written by
`scripts/surface-review --stamp "<note>"`); entries before 2026-10-30 are in
git history (`git log -p docs/runbooks/surface-review.md`).

