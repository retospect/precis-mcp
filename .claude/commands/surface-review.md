---
description: Mine recent agent sessions (local .jsonl + the prod tool_calls ledger + llm_call_log) for where the precis MCP surface confused an agent, wasted tokens, or was missing a capability — then attach measured evidence to the existing backlog item, or file a gripe. The 14-day surface-review pass.
argument-hint: "[window, e.g. '7d' or '14d' — default 14d]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(scripts/mine-sessions/run.sh:*), Bash(uv run scripts/mine-sessions/*), Bash(scripts/surface-review:*), Bash(scripts/docs-index:*), Bash(git log:*), Bash(git show:*), Bash(git merge-base:*), Bash(grep:*), Bash(ls:*), Bash(wc:*), Task, mcp__precis__get, mcp__precis__search, mcp__precis__put
---

Run the MCP **surface review**. Window: `$ARGUMENTS` (default `14d`).
Full procedure, levers and the false-positive gates:
`docs/runbooks/surface-review.md` — read it, this command is the driver.

**Calibration, before anything else.** The measured error rate on this
surface is **0.8%**. Errors are not where the waste is. The signal is
successful-but-clumsy calls: fat renders, N calls where one would do, detours
to raw SQL because no verb exists. If you open with the error histogram you
will find a rounding error and wrongly call the surface healthy.

**Remit.** This pass owns the precis MCP surface — verbs, error messages,
renders, vocabulary, capability gaps. `token-review` owns harness/agent
behaviour; `skill-search-review` owns skill discovery ranking. Cross-
reference at the edges, do not duplicate a finding into two logs.

## 1. Read the known set

The territory is already well-populated — this pass's job is to put
*measurements* on existing items, not to mint more of them.

- `search(kind='gripe', tags=['STATUS:open'])` — **no `q=`**. Adding one
  turns a complete enumeration into a ranked filter and the count silently
  drops.
- `docs/backlog/INDEX.md`, the surface family: `mcp-surface-economy`,
  `capability-discovery-on-a-sprawling-surface`, `vocab-compaction`,
  `token-review-hook-gaps`, `llm-prompt-surface-audit`,
  `mcp-verb-kwarg-parity`, `tick-tool-lists-and-discovery-reflex`,
  `skill-eval-harness`, `context-quality-eval`.

Hold this as the known set. Every candidate later gets marked known/new
against it.

## 2. Mine (scripts only, zero model)

```sh
scripts/mine-sessions/run.sh --since <window> --prod --random 36
```

Size the random arm at 30–40, not the default 10 — see the runbook. Pass #1
ran 12 and the arm still produced the pass's single most valuable finding,
but could only rate it "plausible, n=2".

Produces `out/scoreboard.{md,json}`, `out/candidates.json`, `out/cards/**`.
Drop `--prod` if the cluster hop fails — a local-only run is still a pass,
just say so in the log.

## 3. Read the scoreboard

`scripts/mine-sessions/out/scoreboard.md`. One screen. Read the byte and
retry columns before the error table. Compare against the previous pass's
`scoreboard.json` if one is around — same `schema_version` means the numbers
are comparable.

## 4. Fan out — one agent per detector directory

Dispatch `forensics` agents (sonnet) **in parallel, one message**. Each gets:
its own `out/cards/<detector>/` directory, the detector's "what this means"
line, and the owning handler or skill file. Raw transcripts must not reach
this loop — that is what the cards are for.

**Also dispatch one agent on `out/cards/random/`** with no detector and no
signature, asked only: *what confused this agent?* The catalogue can only
confirm shapes we already thought of; this arm is how a pass finds the ones
we did not. Do not skip it because the detector cards look rich.

Require each agent to return the `forensics` shape —
`signature — frequency — likely cause — next step` — with every finding
classified into a fix class: **skill edit · error-message fix · MCP
capability · render diet · task-template fix · harness-hook fix · doc fix**.

## 5. Gate before filing

Both traps have burned a pass before (gr51426 hit both). Apply to every
finding you are about to file:

- **Demand a real failure.** Only count a match inside a `tool_result` with
  `"is_error":true` tied to a `tool_use.id` — not narration, not a prior
  assistant message, and not a gripe or backlog body *quoting* the error
  string and read back into a fresh tick.
- **Prove it is unfixed.** Pull real occurrence timestamps newest-first. If
  they cluster in the past, find the callsite, `git log -S'<callsite>'` for a
  fix, and check it is deployed
  (`git merge-base --is-ancestor <fix_sha> <deployed_sha>`). If every real
  occurrence predates the fix's deploy it is **already fixed** — a stale
  artifact, not new work. Do not file it.

## 6. File — attach first, mint last

- **Default:** append the measurement to the existing item. A backlog file
  gets a dated section; a gripe gets a comment via
  `put(kind='gripe', id=N, text='…')` (there is no `edit(kind='gripe')`).
- **Mint a gripe** only for a confirmed-live cause nothing covers, and route
  it through the `gripe-filer` agent so its dedup step runs.
- **Fix in-session** the cheap ones: error wording, a skill's vocabulary, a
  stale doc line. These are the pass's highest return per token.

Skim any card text before pasting it into a gripe. `redact.scrub()` is a
filter, not a proof.

## 7. Log

Append one dated line to `docs/runbooks/surface-review.md` `## Log`, newest
first: date, window, corpus size, headline numbers, what shipped, what was
filed. That line is the cadence clock — `scripts/surface-review` reads it.

Re-read `docs/backlog/friction-reflection-enable.md` and say in the log
whether leaving `PRECIS_FRICTION_REFLECT` off is still the right call. It is
the one lever that would turn this retrospective into a continuous feed.

Then ship the in-session fixes with `/go`.
