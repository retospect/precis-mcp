# nanoreactor

## Resume

- **Pillar:** 3d-design
- **Next:** Station-path-finder (`nanoreactor-station-path-finder`); the T1 entry slice (`/rxn` ledger, kept rxn records) is in prod.
- **Blocked by:** Nothing for station-path-finder. T2's tunnel scaffold is built (reservoir sheets + opening solver, 2026-10-14); H2/H3 remain.
- **Unblocks:** The per-station barrier ledger that decides whether the NO → NH3 nanoreactor needs a drive; the reaction movie.
- **Acceptance:** Each item's own acceptance criteria; the thread ends with the T6 movie of a designed nanoreactor.
- **Worktree:** `nanoreactor`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when a designed NO → NH3 nanoreactor plays as a
four-pane movie built from computed station records, every barrier on
the path checked at DFT ([nanoreactor-no-nh3](../nanoreactor-no-nh3.md)).
T0 tabulated thermochemistry shipped in R13/R14, including the radical
fits; the T1 entry slice (`/rxn` web ledger, kept rxn equation sets) is
in prod. Order follows the data: each tool consumes the previous one's
output.
**Worktree:** `nanoreactor`

## Do next

1. **backlog/nanoreactor-station-path-finder.md** — T1. The bare and
   theozyme tiers decide whether a drive is needed, and its station record
   is the contract every later tool reads.
2. **First kept NO → NH3 ledger** (`todo:470273`, owner or root; a prod
   write): keep the four-step NO→HNO→H2NO→NH2OH→NH3+H2O set at 300 K via
   `/rxn` (or the `put` in `precis-rxn-help`) and link its `/refs/rxn/<id>`
   URL here. Prod held no kept rxn record as of 2026-10-15.

## Horizon

1. **backlog/hexfold-nanoreactor-tube.md** — T2; reservoir sheets +
   armchair opening solver landed 2026-10-14 (`tunnel-reservoirs`,
   `hexfold/examples/tunnel_reservoirs.hx`); next ranked H2 (several
   radii along the tube + optional outer Y-junction) then H3 (MLIP
   relax with per-seam strain).
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
