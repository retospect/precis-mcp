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

2. **Fresh fixture, leaks only under an uncommitted cost term — CORRECTED
   2026-09-30.** As first filed, this item said
   `tests/test_pcb_ewod_dogfood.py::test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`
   "fails today" with

   ```
   AssertionError: an escape routed on the electrode layer: {'F.Cu', 'In2.Cu', 'B.Cu'}
   ```

   and that observation 2 was "the one that matters". **That was measured in
   a worktree carrying an uncommitted `routing_area` cost term** (cost.py +
   optimize.py). Reverting just those files and re-running the file gives
   `8 passed`; restoring them reproduces the failure. So the failure was
   never a property of `main`: on `main` this fixture does NOT leak.

   What survives is narrower but still worth knowing: `routing_area` changes
   the placement, and at the placement it chooses an escape lands on `F.Cu`.
   That is either a latent leak reachable only from some placements, or a
   placement-sensitive assertion in the test. Distinguishing the two is the
   first step now, and the reproducer is "apply the `routing_area` WIP",
   not "run the suite".

   **Re-verified 2026-09-30 on top of `realize.pad_board_wh`**, after the
   occupancy grid and the gerber/DRC model were made to agree about a
   synthesized rect pad's board-space orientation. With `routing_area`
   reverted the dogfood file is `8 passed`; with it applied the `F.Cu`
   escape assertion still fails. That eliminates pad geometry as the
   cause — the divergence transposed 45 pads on the esp32c3 board and was
   the obvious suspect for anything measuring escape layers — and leaves
   the failure attributable to the term's placement, which is the question
   Acceptance asks. The same fix removed the other thing held against the
   term: `tests/test_pcb_reference_end_to_end.py` is now 5 passed at every
   seed WITH `routing_area` applied.

This correction downgrades the item. Observation 1 (the stale stored class
on `pb345846`) is real but is the already-tracked "`op='route'` never
re-runs the generator" staleness, owned by
`pcb-generator-version-is-a-manual-bump-with-no-tripwire.md`. Nothing here
demonstrates a leak on a board born today from current `main`, which is
what the original rank was based on.

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

- `test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap` passes
  **with the `routing_area` cost term applied** — on `main` alone it
  already passes, so a green run without that term is the vacuous case
  here, not evidence.
- The first question answered explicitly, in the file: is the `F.Cu` escape
  under `routing_area` a real leak at that placement, or a
  placement-sensitive assertion? Assert the `allowed` set handed to the
  maze equals the net class's `layers` — that separates "realization
  ignored the class" from "the class was right and the test is reading
  placement".
- A negative control: force the net class to include `F.Cu` and confirm the
  realized escape layers then *do* include it — otherwise the test is
  vacuous for the reason the assertion is there.

test: `tests/test_pcb_ewod_dogfood.py::test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`
