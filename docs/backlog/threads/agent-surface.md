---
roster: active
pillar: platform
effort: medium
---
# agent surface — MCP effectiveness

## Resume

- **Next:** [tool-description-budgets](../tool-description-budgets.md) slice 1 (the cap test into `CODE_HYGIENE`); the gr494831 error contract (M1–M3) is on main, so it unblocks nothing further.
- **Blocked by:** Nothing.
- **Unblocks:** Agents that recover from a refused call without a human, and fewer silent misuses on the surface every thread uses.
- **Acceptance:** Each item's own criteria; for gr494831, a call either does exactly what its ack says or refuses with a next step.
- **Worktree:** `agent-surface`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when an agent on the precis MCP never has a misuse succeed
silently, every refusal carries the fix (`next=`/`options=`), and the tool
descriptions fit their byte budget with kind detail in skills — the agent
half of the Surfaces axis in `docs/roadmap.md`. The gr494831 error
contract (M1–M3) is on main in full; the web Drive items (the human half
of the same surface) follow the agent-side work; the order is now
description budgets, then Drive.
**Last reviewed:** 2026-10-17
**Worktree:** `agent-surface`
**Allocation decision:** active — Reto 2026-10-17 ("we control the mcp, let's make it awesome").

## Do next

1. **backlog/tool-description-budgets.md** — verb descriptions sit at
   966–971 of 1024 bytes; slice 1 (the cap test into `CODE_HYGIENE`) can go
   first, the rest waits on it so kind clauses move into skills once refusals
   carry the fix.
2. **backlog/drive-slash-command-line.md** — `/` in the Drive search box
   opens a command popdown; the web counterpart of discoverable verbs.
   Slice 2 (bare-word qualifiers + `//` escape) landed 2026-10-21;
   remaining slices are pipes (traverse) and result-list actions.

## Horizon

1. **backlog/mcp-seen-state-dedup.md** — the server answers a repeat
   read with a `^^` back-reference; first in Horizon because tool-result
   carry is the largest agent token cost, after the M3 error contract
   because it waits on `multi-user-overhaul.md` S10.0 + S10b.
2. **backlog/drive-filter-hierarchy.md** — Return and Showcase remain;
   Return needs personal viewing history.
3. **backlog/drive-my-vs-agent-stuff.md** — separate a user's refs from
   their agent's; follows the Drive ruling that accepted agent writes.
4. **backlog/drive-presenter-completeness.md** — presenter contract
   totality and the kind-taxonomy audit; makes every kind render in Drive.
5. **backlog/drive-characteristic-scale.md** — a derived scale value for
   design refs as a sort key; blocked on a ruling (moved from
   multiscale-design-core 2026-10-17).

## Parked

- (none)

## Seam

`session-mcp-shared-server` owns serving the MCP (process, restarts,
connectivity); this thread owns what a call on it says and refuses.
`ship-gate-ci` keeps the hygiene lane the description cap test runs in.
