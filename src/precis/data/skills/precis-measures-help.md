---
id: precis-measures-help
title: precis — PCB measures (the measuring tapes)
summary: state placement & layout intent as measures the autoplacer optimises and the eyes evaluate — keep the regulator away from the antenna, the bypass cap AT the pin, this part under 3mm tall. Covers separation, proximity, height, role-based selection, hard/soft/gauge strength.
applies-to: put (kind='pcb') measures[]; get(view='measures')
status: active
---

# precis-measures-help — design intent as measuring tapes

A **measure** is a stretch of intent between parts of a `pcb` design — "keep
these apart", "keep this close", "stay under this height". The autoplacer
**optimises** the soft/hard ones and the eyes **evaluate** all of them
(`view='measures'`). They make the *why* of a layout explicit and checkable
instead of hiding it in coordinates. Add them in the `measures` array of `put`
([[precis-pcb-help]]).

```python
put(kind='pcb', id='s', args={'measures':[
  {'metric':'separation',
   'operands':[{'role':'sensitive'},{'role':'noisy'}],
   'goal':10, 'strength':'soft',
   'reason':'keep the mic preamp off the switching regulator'},
  {'metric':'proximity',
   'operands':[{'instance':'C1'},{'instance':'U1'}],
   'goal':2, 'strength':'hard',
   'reason':'VDD bypass must sit right at the pin'},
  {'metric':'height',
   'operands':[{'role':'under_lid'}],
   'goal':3.0, 'strength':'gauge',
   'reason':'clearance under the enclosure lid'},
]})
```

## Anatomy

- **metric** — what's measured (table below).
- **operands** — the parts it spans, selected by **instance** (`{'instance':
  'U1'}`) or by **role class** (`{'role':'sensitive'}` → every instance tagged
  `sensitive`). Role selection is the power move: tag parts `roles:['noisy']` /
  `['sensitive']` / `['under_lid']` at `put` time, then write one measure over
  the class.
- **goal** — the target value (mm for geometry).
- **strength** — `hard` (must hold; heavily penalised in placement), `soft`
  (optimised, traded against crossings/length), `gauge` (measured + reported
  only, never drives placement — a ruler, not a constraint).
- **direction** — optional: which side of `goal` is ok. `min`/`keep_above`
  (value must stay ≥ goal), `max`/`keep_below` (≤ goal), `target` (aim at
  goal, ±10%). Without it each metric keeps its natural sense (the table
  below). Flips both the verdict *and* the placement pull.
- **weight** — optional: scales a soft measure's pull (default 1.0;
  `weight: 0` records the measure without letting it steer placement).
- **reason** — required-in-spirit free text; this is the design rationale the
  next reader (or you, later) needs.

## Metrics

| metric | evaluates | drives placement | use for |
|--------|-----------|------------------|---------|
| `separation` | min centroid gap ≥ goal | **yes** | keep noisy ↔ sensitive apart |
| `proximity` | max centroid gap ≤ goal | **yes** | bypass cap at the pin; crystal at the MCU |
| `height` | each part's height ≤ goal | reports | fit under a lid / next to a connector |
| `parallelism` | bus traces run together | pending | I²C/SPI grouping (phase 2) |
| `supply_path` | short/low-Z power route | pending | power integrity (phase 2) |
| `topology` | net wiring order | pending | daisy-chain vs star (phase 2) |
| `plane_continuity` | unbroken return | pending | ground integrity (phase 2) |
| `thermal` | spread heat | pending | regulators / LEDs (phase 2) |

The first three (the placement-geometry ones) **evaluate now**; the
connectivity metrics are **stored and reported `pending`** until their
evaluators land — write them anyway, they document intent and will light up
later.

## Evaluate — `get(view='measures')`

```python
get(kind='pcb', id='s', view='measures')
# metric · strength · goal · value · verdict(ok/violated/pending) · reason
```

A `hard` violation is a real problem; a `soft` one is a tradeoff the placer
made; a `gauge` row is just information. Re-run after `autoplace` to see what
held.

## How measures interact with placement

`autoplace` minimises `W_CROSS·crossings + length + W_MEASURE·penalty`, where
the penalty is the violation of `soft`/`hard` separation & proximity measures.
So measures **steer** the layout, they don't post-hoc reject it. `fixed` parts
(connectors, mounting holes) still never move regardless. See
[[precis-pcb-help]] for the place loop.

## Idioms

- **Bypass at the pin:** `proximity` `hard`, cap ↔ IC, goal ~2 mm. (See
  [[precis-decoupling-help]].)
- **Quiet analog:** tag the preamp `sensitive`, the regulator/MCU `noisy`, one
  `separation` `soft` between the roles.
- **Fits the box:** tag lid-side parts `under_lid`, one `height` `gauge`.
- **Crystal hugs the MCU:** `proximity` `soft`, crystal ↔ MCU, small goal.
