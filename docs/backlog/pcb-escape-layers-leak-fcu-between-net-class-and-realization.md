---
status: draft
title: escapes route on F.Cu even though the generator refuses to author it
prio: high
---

# escapes route on F.Cu even though the generator refuses to author it

## Motivation / why

On an EWOD board the electrode layer (`F.Cu`) is the *fluid* side: copper
there is an electrode, not a signal. An escape strand placed on `F.Cu`
crosses the electrode field and is a real fabrication defect, not an
aesthetic one.

Two independent observations, both current:

1. **Live prod board.** `pb345846`'s stored net class is

   ```
   ewod_ARR1_escape  {"layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "clearance_mm": 0.099}
   ```

   `F.Cu` is in the allowed set, so its router is permitted to put escape
   copper on the electrode layer. A board created today from the same
   generator and the same params derives `{"layers": ["B.Cu"]}` correctly —
   so the stored class is *stale*, not wrong-by-construction. The generator
   has derived the list since 2026-09-27; `op='route'` never re-runs the
   generator, so an older board keeps whatever class it was born with.

2. **Fresh fixture, still leaks.**
   `tests/test_pcb_ewod_dogfood.py::test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`
   fails today with

   ```
   AssertionError: an escape routed on the electrode layer: {'F.Cu', 'In2.Cu', 'B.Cu'}
   ```

   This one is NOT the stale-class story — the fixture is generated in the
   test process. So there is a second path by which `F.Cu` reaches a
   realized escape.

Observation 2 is the one that matters: it means the defect is reachable on
a board born today, not only on `pb345846`.

## What is already known

- `generators.py` rejects `F.Cu` in *authored* `escape_layers` (search the
  module for the escape-layer validation). So the authored path is closed.
- The maze honours a per-net `allowed` layer list (`precis/pcb/maze.py`,
  the per-net allowed-layer filter in the neighbour expansion). So if the
  net class says `B.Cu` only, the maze should not emit `F.Cu`.
- Therefore the leak is between **the net class the generator derives** and
  **the `allowed` set the router actually receives** — one of: the class is
  not what the test's own class query reports; the escape nets are not
  matched to `ewod_ARR1_escape` by name; or the escape realization path
  bypasses the net class entirely and uses the board stackup.

That last possibility is the cheapest to falsify first: assert, inside the
route path, that the `allowed` set handed to the maze for an escape net
equals the net class's `layers`.

## In scope

- Root-cause which of the three the leak is, on the *fresh fixture* (not on
  `pb345846`, whose stale class is a separate and easier story).
- Fix it so a generated EWOD board cannot realize escape copper on the
  electrode layer.
- Keep the failing dogfood test as the regression guard.

## Explicitly NOT in scope

- Re-deriving net classes for existing boards. `pb345846` needs a
  regenerate, which is a destructive prod write and needs Reto's per-write
  go-ahead. File that separately if the fix lands first.
- Any change to what layers the generator *derives* — the derivation is
  correct today (`{"layers": ["B.Cu"]}` on a board created 2026-09-30).

## Acceptance

- `test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap` passes.
- A negative control: force the net class to include `F.Cu` and confirm the
  realized escape layers then *do* include it — otherwise the test is
  vacuous for the reason the assertion is there.

test: `tests/test_pcb_ewod_dogfood.py::test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`
