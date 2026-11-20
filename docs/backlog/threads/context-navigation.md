---
roster: active
pillar: platform
effort: medium
design-review: yes
---
# context navigation — the twin, sets, row grammar, Mode B

## Resume

- **Next:** the autoskill deploy verification, then A0 (context-navigation (me690580) §2). Stop after A0 until the afternoon of hook logging (§7) is done.
- **Blocked by:** Nothing for Do-next 1–2. The twin (Do-next 3) waits on the hook-logging check.
- **Unblocks:** Every agent read on the MCP: seen-stubs, sets, the compaction handoff, the N1 meters, autoskill pointers, Mode B.
- **Acceptance:** §1 meters exist and move; the eval baseline is recorded before targets are set.
- **Worktree:** `context-navigation`
- **Builds:** A0 one; twin two; readers one each; Mode B B1–B4 about one each. Unknown for the eval bank.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when an agent reaches a correct answer with the fewest result bytes and hops, every needed block present, and the meters in `context-navigation (me690580)` §1 say so — the agent half of the Surfaces axis in `docs/roadmap.md`, shared with `agent-surface`. Today the spec is ruled (2026-10-30) and nothing is built; the list follows the spec's §10 order, dependency first.
**Last reviewed:** 2026-10-30
**Worktree:** `context-navigation`
**Allocation decision:** active — Reto 2026-10-30 (as recommended: runway ends after A0 until the hook check).

## Do next

1. **me691549** deploy verification — new dream/plan_tick `tool_calls` rows carry `agentlog_id` and `source`, `autoskill_sessions.used` not null. First because the meters read these columns.
2. **me690580 §2 A0** — code landed; verify after deploy that doctor_tick/review/asa_bot `tool_calls` rows carry `agentlog_id` and `source`, then close the gripe.
3. **me690580 §7 twin** — `context_items` + `_ctx` pop + hooks; replaces four stores; everything below reads it. Waits on the hook-logging check.
4. **me690580 §4 + §7 readers** — stubs, `rs` sets, compaction handoff, meters, eval bank, baseline.

## Horizon

1. **me690580 §2** — autoskill pointer rows on the twin; the hold-out comparison starts here, not at deploy.
2. **me690580 §3 → §5** — row grammar, then multi-get + injection, then multi-query on every kind; unblocks held WU2/WU5/WU7.
3. **me694125** — its slices re-pointed at sets and the row grammar; the authoring lint is independent and can go any time.
4. **me690580 §9** — cache-backed chunking + backfill; makes the big paid results navigable.
5. **me690580 §8 B0** — asa preamble reorder, half a build, its own meter; any time.
6. **me690580 §8 B1–B4** — Mode B loop; plan_tick first.
7. **me690567** — merge into the twin reader once it exists; pointer note until then.

## Parked

- **REF_BYTES trigger shape** (`cite-s1`) — row-level trigger on `chunks` vs statement-level vs writer-side; unparks when the Opus review rules.
- **Subagent `session_id` constancy** — unparks after one afternoon of PreToolUse logging; gates Do-next 4.

## No action needed

- **Gripe title cut** (`block-navigation (me694125)` R12) — ruled 2026-10-30 (cut silently, full line in body), in build elsewhere.
