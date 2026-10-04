# nanoreactor

## Resume

- **Pillar:** 3d-design
- **Next:** When activated, review and mark [reaction-energetics-ledger](../reaction-energetics-ledger.md) `ready`, then ship it.
- **Blocked by:** No declared active owner (dormant, filed 2026-10-04). T2 waits on hexfold's k = 3 seams.
- **Unblocks:** The per-station barrier ledger that decides whether the NO → NH3 nanoreactor needs a drive; the reaction movie.
- **Acceptance:** Each item's own acceptance criteria; the thread ends with the T6 movie of a designed nanoreactor.
- **Worktree:** `nanoreactor`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when a designed NO → NH3 nanoreactor plays as a
four-pane movie built from computed station records, every barrier on
the path checked at DFT ([nanoreactor-no-nh3](../nanoreactor-no-nh3.md)).
Today the chain is specced through T1 and T6; nothing is built. Order
follows the data: each tool consumes the previous one's output.
**Last reviewed:** 2026-10-04
**Worktree:** `nanoreactor`

## Do next

1. **backlog/reaction-energetics-ledger.md** — T0; ships on its own and
   gives every later step its thermodynamic reference.
2. **backlog/nanoreactor-station-path-finder.md** — T1; the bare and
   theozyme tiers decide whether a drive is needed, and its station record
   is the contract every later tool reads.

## Horizon

1. **backlog/hexfold-nanoreactor-tube.md** — T2; waits on k = 3 seams
   (hexfold-toolkit owns the build).
2. **backlog/nanoreactor-no-nh3.md §T3** — track designer; spec once
   T1's station record exists.
3. **backlog/nanoreactor-no-nh3.md §T4** — ring/pendant designer; spec
   once T3 exists.
4. **backlog/nanoreactor-no-nh3.md §T5** — assembled QM/MM check; needs
   T2 + T4.
5. **backlog/nanoreactor-movie.md** — T6; renders T1 records first,
   T5 records later.

## Parked

- **backlog/cnt-channel-staged-catalysis.md** — the general channel
  concept and its reading list; unparks with T2.
