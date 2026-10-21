---
roster: active
pillar: 3d-design
effort: medium
---
# nanoreactor

## Resume

- **Next:** Pocket-from-molecule slice 3 (held-shape spec, `pocket-from-molecule` — slices 1–2 shipped 2026-10), then station-path-finder (`nanoreactor-station-path-finder`); the T1 entry slice (`/rxn` ledger, kept rxn records) is in prod.
- **Blocked by:** Station-path-finder is a draft: it needs a `ready` pass and slicing first. T2's tunnel scaffold is built (reservoir sheets + opening solver, 2026-10-14; H2 steps and H3 relax since); the H2 cone's relaxed washer-zone gate remains open.
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
**Allocation decision:** active — Reto 2026-10-17; owns `pocket-from-molecule`, the driver of Pillar 2's intent → realize loop.

## Do next

1. **backlog/pocket-from-molecule.md** — Reto 2026-10-17, top rank: the
   per-station pocket spec (NO attractive pocket, held NH2) and the
   GFN2 → MLIP → DFT realize loop that turns it into a tube wall.
   Slice 1 built 2026-10-17 (xtb charge/multiplicity, se
   `view='surface'`, provisional Q4 settings). Q2 (tethered-ring wall
   chemistry) and Q11 (H-addition requirement) answered 2026-10-17;
   slice 2 needs Q8 and Q9 first.
2. **backlog/nanoreactor-station-path-finder.md** — T1. The bare and
   theozyme tiers decide whether a drive is needed, and its station record
   is the contract every later tool reads. Still a draft: it needs a
   `ready` pass and slicing (engine work sits in autocatpath) before a
   build.

The first kept NO → NH3 ledger at 300 K is `rxn:no-nh3-ledger-300k`
(reader `/refs/rxn/489242`, `todo:470273`).

## Horizon

1. **backlog/hexfold-nanoreactor-tube.md** — T2; reservoir sheets +
   armchair opening solver landed 2026-10-14 (`tunnel-reservoirs`,
   `hexfold/examples/tunnel_reservoirs.hx`); H2 stepped rail tubes
   build with `step: washer|cone` (cone landed 2026-10-21), left: the
   cone's relaxed gate (§"H2 cone gate follow-up": 18→8 fixture,
   uncapped per-zone counts, judged after a real-rung `hexfold_relax`
   job; prod relax jobs allowed any time, Reto 2026-10-21); H3 relax
   landed 2026-10-19.
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
