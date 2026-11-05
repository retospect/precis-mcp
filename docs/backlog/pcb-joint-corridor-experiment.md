# Bounded joint routing experiment

Reto approved the next plan on 2026-11-04, including the same-direction
bundle and indirect blocker hypotheses assessed by Astra. First establish
whether the existing router already solves the proposed small examples.

Build at most two tiny physical fixtures: A needs corridor 1; incumbent B
uses 1 but may use 2; incumbent C uses 2 but may use 3. The known legal
joint witness is A1/B2/C3. A second example has two parallel nets and two
legal lanes through a shared bottleneck. Use actual pad/keepout geometry,
layers, widths, clearance and emitted copper, with independently checked
simultaneous connectivity and DRC. Mocked grow results are not a physical
capacity witness. Include an untouched D and a genuinely overfull control.

Run the pinned current search first with its unchanged budget. If it
already solves a fixture, record that the fixture does not demonstrate
the proposed missing capability; do not strengthen it repeatedly until
it fails. Stop if the supposed witness is physically invalid.

Only for a baseline failure with a verified witness, prepare the smallest
subsequent comparison: baseline, depth-two blocker closure, joint corridor/
layer/lane proposals, and both, under equal charged grow/evaluation budgets.
Retain the existing final failure-set, terminal and DRC gate. Do not alter
production search defaults in the fixture-building step. No private board
data, matrix, S3 capture, stackup change, manufacturing or deployment.

## Bounded fixture result

On pinned source `e125983de`, both tiny examples already complete using
the current search. `tests/fixtures/pcb/joint_corridors.py` defines actual
0.9 mm square terminal pads, F.Cu-only classes, keepouts, trace widths and
simultaneous witness paths. `tests/test_pcb_joint_corridors.py` emits those
witnesses with the ordinary realiser, checks shared connectivity/geometric
DRC and independently checks swept copper against keepouts. The baseline
uses `route_design` and counts every real `_grow_net` call, including
ordinary growth and retries, with the unchanged 20-per-net shove setting.

| Example | Failed set | Grow calls | Tracks | Vias | DRC errors |
| --- | --- | ---: | ---: | ---: | ---: |
| ABC, separate D | empty | 4 (one per net) | 4 | 0 | 0 |
| Parallel two lanes | empty | 2 (one per net) | 2 | 0 | 0 |
| Overfull 0.50 mm gate | A, B | 2 | 0 | 0 | 0 |

The ABC openings are 0.95/0.75/0.55 mm for traces 0.60/0.40/0.20 mm,
all at 0.15 mm clearance. D occupies a separate compartment. The parallel
gate is 1.0 mm wide with 0.20 mm traces at offsets ±0.20 mm; two need
0.85 mm including both wall clearances and pair clearance. A centred
first trace leaves no second legal centre position. The overfull control
is below the two-track requirement; its exact-width single-track fit is
also rejected with the router's fabrication reserve.

The first parallel construction reused 0.9 mm pads at 0.4 mm pitch:
shared DRC rejected the proposed witness before baseline. One explicitly
approved construction correction moved the pads 2 mm apart and fanned
the witness into the same bottleneck; no failing-case search followed.
An initial ABC measurement completed routing but its report accessed an
unavailable `Board.name`; the corrected reporting run is tabulated above.

These fresh-start results do not demonstrate the proposed missing search
capability, so no four-way implementation/comparison follows from them.
One additional, approved order observation on the same ABC geometry used
the existing `_net_order` test seam for B,C,A,D. The first real grows chose
B2 (y=6), C3 (y=9), A1 (y=3), D (y=13.5): four grows total, no failed
nets, zero DRC errors, no shove swaps/partners. It did **not** produce the
B1/C2 incumbent. The indirect-blocker hypothesis remains untested; this
is capacity/order sanity evidence, not evidence that closure is redundant.
A controlled-incumbent architecture seam needs an explicit design before
claiming that recovery experiment. No engine or default changed.
