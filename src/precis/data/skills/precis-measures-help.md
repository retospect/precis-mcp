---
id: precis-measures-help
title: precis — PCB measures (the measuring tapes)
summary: state placement & layout intent as measures the autoplacer optimises and the eyes evaluate — keep the regulator away from the antenna, the bypass cap AT the pin, this part under 3mm tall. Covers separation, proximity, align, height, role-based selection, hard/soft/gauge strength.
answers:
  - how do I state placement or layout intent as a measure the autoplacer can optimise?
  - how do I evaluate a design's current measures?
  - how do measures interact with placement?
applies-to: put (kind='pcb') measures[]; get(view='measures')
tags: [design]
kinds: [pcb]
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
put(
    kind="pcb",
    id="s",
    args={
        "measures": [
            {
                "metric": "separation",
                "operands": [{"role": "sensitive"}, {"role": "noisy"}],
                "goal": 10,
                "strength": "soft",
                "reason": "keep the mic preamp off the switching regulator",
            },
            {
                "metric": "proximity",
                "operands": [{"instance": "C1"}, {"instance": "U1"}],
                "goal": 2,
                "strength": "hard",
                "reason": "VDD bypass must sit right at the pin",
            },
            {
                "metric": "height",
                "operands": [{"role": "under_lid"}],
                "goal": 3.0,
                "strength": "gauge",
                "reason": "clearance under the enclosure lid",
            },
        ]
    },
)
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
| `align` | pos_b − pos_a = offset on the chosen axes, within goal (mm tolerance) | **yes** | pin a part to a datum; line two parts up on a row/column |
| `height` | each part's height ≤ goal | reports | fit under a lid / next to a connector |
| `parallelism` | bus traces run together | pending | I²C/SPI grouping (phase 2) |
| `supply_path` | short/low-Z power route | pending | power integrity (phase 2) |
| `topology` | net wiring order | pending | daisy-chain vs star (phase 2) |
| `plane_continuity` | unbroken return | pending | ground integrity (phase 2) |
| `thermal` | spread heat | pending | regulators / LEDs (phase 2) |

The first four (the placement-geometry ones) **evaluate now**; the
connectivity metrics are **stored and reported `pending`** until their
evaluators land — write them anyway, they document intent and will light up
later.

## `align` — line a part up with a part or a fixed datum

Two operands, the proximity shape: each is `{'instance': 'D3'}`, a board
feature `{'feature_id': 17}` (e.g. a mounting hole) or an absolute point
`{'point': [x, y]}` (mm), and at least one must be a part. Features and points
never move, so the part is the one that gets steered. `axis` and `offset` go in
the row's `meta`, not on the operands.

```python
{
    "metric": "align",
    "operands": [{"instance": "D3"}, {"instance": "J1"}],
    "meta": {"axis": "x", "offset": [0.0, 1.5]},
    "goal": 0.05,          # tolerance in mm; null means 0.05
    "strength": "hard",
    "reason": "LED D3 sits on J1's centre line, 1.5 mm below it",
}
```

- **offset** (`meta.offset`, `[dx, dy]` mm, default `[0, 0]`): operand 2
  relative to operand 1, `pos_2 − pos_1 = offset`. With `[0, 1.5]` the second
  operand sits 1.5 mm above (larger y than) the first. Swapping the two
  operands negates the offset; with no offset the order is irrelevant.
- **axis** (`meta.axis`, default `xy`): `x` means only the x coordinates must
  agree (y free), `y` is the mirror case, `xy` constrains both.
- **value** = Euclidean norm of the residual `pos_2 − pos_1 − offset` over the
  constrained axes; `ok` iff value ≤ goal.
- **position** = footprint origin, as placed in EasyEDA — not a pad, not the
  courtyard centre. To align an optical centre (an LED's lens, a button cap),
  put the origin-to-centre vector into `offset`.
- `hard`/`soft` price it linearly per mm above the tolerance (`hard` ~40×
  `soft`), part-to-part like `proximity`, part-to-feature/point as a pull
  against a fixed pseudo-part. `gauge` never steers.
- **hard snap:** the anneal's step floor is 0.5 mm, so after the anneal a
  `hard` align that is within 0.5 mm of satisfied, with exactly one free
  operand, is closed exactly: that part (with its rigid group) moves by the
  residual, provided the new spot is inside the outline and clear of other
  courtyards, holes and authored vias. If it is not, the part stays and the
  measure reads `VIOLATED` with its residual. `view='measures'` shows
  `detail = snapped` on a row the last place/route run closed that way (a
  `soft` align never snaps). A design with a hard/soft align to a fixed
  point or feature is not re-centred in the outline after placement.
- An operand that cannot resolve (unknown or unplaced refdes, unknown
  `feature_id`, a malformed `point`/`offset`/`axis`) reads `pending` in
  `view='measures'`, never `ok`.

Not supported: `role` operands (a role measure is `pending`), two fixed
datums (nothing to move), rotation (only x/y position is aligned, not the
part's angle), snapping when both parts are free, and operands that belong to
a different design.

## Evaluate — `get(view='measures')`

```python
get(kind="pcb", id="s", view="measures")
# metric · strength · goal · value · verdict(ok/violated/pending) · reason
```

A `hard` violation is a real problem; a `soft` one is a tradeoff the placer
made; a `gauge` row is just information. Re-run after `autoplace` to see what
held.

## How measures interact with placement

The production anneal (the enqueued `op='place'` job, and `op='route'`'s
re-place pass) prices each `soft`/`hard` proximity, separation & align measure as
a cost term: zero when satisfied, growing linearly with violation-mm, with
`hard` weighted ~40× `soft`. So measures **steer** the layout, they don't
post-hoc reject it — a `hard` measure is a strong pull, not a legality
wall, and on a congested board it can still lose (read `view='measures'`
after placing to see what held). The quick `autoplace` path prices them
too (`W_MEASURE·penalty`). `fixed` parts (connectors, mounting holes)
still never move regardless. See [[precis-pcb-help]] for the place loop.

## Idioms

- **Bypass at the pin:** `proximity` `hard`, cap ↔ IC, goal ~2 mm. (See
  [[precis-decoupling-help]].)
- **Quiet analog:** tag the preamp `sensitive`, the regulator/MCU `noisy`, one
  `separation` `soft` between the roles.
- **Fits the box:** tag lid-side parts `under_lid`, one `height` `gauge`.
- **Crystal hugs the MCU:** `proximity` `hard`, crystal ↔ MCU, small goal —
  and the load caps get their own `proximity` `hard` onto the crystal.
