---
status: ready
pillar: 3d-design
thread: se-machine-design
prio: low
title: se flagellar motor — a rotary-motor component with a linear torque-speed model, driven by proton-motive force
---

# se flagellar motor (rotary biomolecular motor component)

## Why

gr543573: the se kind has no way to model a flagellar motor as a component
for simulation. A flagellar motor is a stator-rotor pair that converts a
proton (or Na+) motive force into torque, with a characteristic
torque-speed curve and CheY-driven direction switching. The se machine-design
thread already names "a motor as a child of one design" as an end state —
this is the smallest component that earns it: the stator-rotor coupling is a
`revolute` connect, the motor's physics is a declared-measure parameter set
plus one COMPUTERS measurand, and the se check layer can then answer "how
much torque at this speed" for any design that contains one.

## Slice 1 — linear torque-speed motor

*Landed 2026-10-31 — the commit carrying this line.*

A rotary motor is two blocks joined by a `revolute` joint (existing
grammar, nothing new): a stator block and a rotor block, `connect`ed
port-to-port with `joint={"class": "revolute", "axis": […]}`. The motor's
parameters are `add_measure` rows on the stator block carrying new
measurands:

- `stator-count` (count) — number of torque-generating stator units.
- `proton-motive-force` (mV) — the PMF input driving the motor.
- `stall-torque` (N·m) — torque at zero speed.
- `zero-load-speed` (rad/s) — speed at zero torque.
- `operating-speed` (rad/s) — the query point.
- `output-torque` (N·m) — the computed output: `output-torque` enters
  `precis_se.properties.COMPUTERS`, so `view='drc'`/`view='measures'`
  realises it like the flow measurands do.

The model is the linear torque-speed curve
`torque(ω) = stall_torque · (1 − ω / zero_load_speed)`. The computer reads
the block's declared measures, returns `None` with a `reason` when an input
is absent (the existing `measure_uncomputable` path), and scales nothing by
stator-count or PMF yet — both are recorded parameters the later slices
consume; declaring them now keeps the measure schema stable.

**Files:**

- `src/precis_se/motor.py` — new: `torque_at_speed(stall, w0, w)` pure
  function + `MOTOR_COMPUTERS` (`{"output-torque": _Computer(…)}`), the
  `flow.py::FLOW_COMPUTERS` shape.
- `src/precis_se/properties/__init__.py` — `_LAZY_TABLES["motor"]` +
  `COMPUTERS.update` for `output-torque`.
- `src/precis/migrations/<next>_se_motor_measurands.sql` — seed the six
  measurands, `INSERT`-only shape of `0231_se_species_measurands.sql`
  (status `proposed`, `specialises` → the `measurand` root).

**Test:** `tests/test_se_motor.py` — a stator block declaring stall torque
and zero-load speed returns `stall/2` at `ω₀/2`, `0` at `ω₀`, `stall` at
`0`; missing `operating-speed` → computer `None` + reason.

## Later (not specced)

- Non-linear torque-speed knee (the real two-regime bacterial curve).
- Na+-motive-force input beside PMF.
- CheY-P-driven direction switching with a rotational-bias parameter.
- Stator remodelling (count varying with load).

## Explicitly NOT in scope

Molecular-level structure of the motor (no MotAB/FliG geometry, no atomic
mode) and ion-channel kinetics (stator conductance, proton hopping).

## Acceptance criteria

`tests/test_se_motor.py` passes: a declared stator/rotor revolute pair with
the parameter measures yields the linear torque-speed value at the declared
operating speed through `COMPUTERS`, and an absent `operating-speed`
surfaces `measure_uncomputable` with a reason.
