---
roster: active
pillar: platform
effort: medium
design-review: yes
---
# context navigation — the twin, sets, row grammar, Mode B

## Resume

- **Next:** [context-navigation](../context-navigation.md) §0 first fix (drop the size refusal), then the autoskill deploy verification and A0 (§2). Stop after A0 until the afternoon of hook logging (§7) is done.
- **Blocked by:** Nothing for Do-next 1–3. The twin (Do-next 4) waits on the hook-logging check.
- **Unblocks:** Every agent read on the MCP: seen-stubs, sets, the compaction handoff, the N1 meters, autoskill pointers, Mode B.
- **Acceptance:** §1 meters exist and move; the eval baseline is recorded before targets are set.
- **Worktree:** `context-navigation`
- **Builds:** §0 half a build; A0 one; twin two; readers one each; Mode B B1–B4 about one each. Unknown for the eval bank.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when an agent reaches a correct answer with the fewest result bytes and hops, every needed block present, and the meters in `context-navigation.md` §1 say so — the agent half of the Surfaces axis in `docs/roadmap.md`, shared with `agent-surface`. Today the spec is ruled (2026-10-30) and nothing is built; the list follows the spec's §10 order, dependency first.
**Last reviewed:** 2026-10-30
**Worktree:** `context-navigation`
**Allocation decision:** active — Reto 2026-10-30 (as recommended: runway ends after A0 until the hook check).

## Do next

1. **backlog/context-navigation.md §0** — drop the over-cap refusal, head + outline above the transport cap; the top failure in the 2026-10-30 census and it blocks nothing.
2. **backlog/autoskill.md** deploy verification — new dream/plan_tick `tool_calls` rows carry `agentlog_id` and `source`, `autoskill_sessions.used` not null; closes gr537773. First because the meters read these columns.
3. **backlog/context-navigation.md §2 A0** — agentlog on doctor_tick, review, asa_bot; without it the baseline undercounts exactly the runners Mode B changes.
4. **backlog/context-navigation.md §7 twin** — `context_items` + `_ctx` pop + hooks; replaces four stores; everything below reads it. Waits on the hook-logging check.
5. **backlog/context-navigation.md §4 + §7 readers** — stubs, `rs` sets, compaction handoff, meters, eval bank, baseline.

## Horizon

1. **backlog/context-navigation.md §2** — autoskill pointer rows on the twin; the hold-out comparison starts here, not at deploy.
2. **backlog/context-navigation.md §3 → §5** — row grammar, then multi-get + injection, then multi-query on every kind; unblocks held WU2/WU5/WU7.
3. **backlog/block-navigation.md** — its slices re-pointed at sets and the row grammar; the authoring lint is independent and can go any time.
4. **backlog/context-navigation.md §9** — cache-backed chunking + backfill; makes the big paid results navigable (gr540306).
5. **backlog/context-navigation.md §8 B0** — asa preamble reorder, half a build, its own meter; any time.
6. **backlog/context-navigation.md §8 B1–B4** — Mode B loop; plan_tick first.
7. **backlog/compact-handoff.md** — merge into the twin reader once it exists; pointer note until then.

## Parked

- **REF_BYTES trigger shape** (`cite-s1`) — row-level trigger on `chunks` vs statement-level vs writer-side; unparks when the Opus review rules.
- **Subagent `session_id` constancy** — unparks after one afternoon of PreToolUse logging; gates Do-next 4.

## No action needed

- **Gripe title cut** (`block-navigation.md` R12) — ruled 2026-10-30 (cut silently, full line in body), in build elsewhere.
