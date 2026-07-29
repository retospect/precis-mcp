---
id: precis-material-help
title: precis — the material kind (CRC-handbook-style properties store)
summary: engineering material properties with per-value sources — entity vs value writes, the canonical-unit rule, the property registry (core/proposed), and the range-filter search
applies-to: get/put/search (kind='material')
status: active
---

# precis-material-help — sourced material properties, canonical units only

`material` is a small star schema: a **material entity** (name/aliases/
class), a typed **property registry** (`material_properties`), and a
**value fact table** (`material_values`) — one row per sourced measurement.
Multiple values per `(material, property)` is a feature: the handbook shows
the spread across sources/conditions, and nobody picks a canonical number
at write time.

**v1 is canonical-units-only.** There is no `pint`, no unit conversion, no
`units=` read param, no off-sample estimate/interpolation, and no `model`
value-type — those are deferred follow-ons. Every property declares one
canonical unit; a write in any other unit is rejected, naming the
canonical one.

## Two writes share one `put`

`put(kind='material', ...)` does one of two things, discriminated by
whether `property=` is present.

### 1. The entity (name / aliases / class)

```python
put(
    kind="material",
    id="6061-t6",
    title="Aluminum 6061-T6",
    meta={
        "material_class": "metal",
        "aliases": ["AA6061-T6", "aluminium alloy 6061"],
        "composition": "Al, Mg 1.0%, Si 0.6%, Cu 0.28%",
    },
)
```

`id=` is a slug you pick — a material may be named at whatever grade
granularity is conventional (finer axes, e.g. temper, live in `conditions`
on the value rows). Calling `put` again on the same `id=` **updates** the
entity: `title=` overwrites if given, `meta=` shallow-merges (adding an
alias doesn't clobber an already-recorded `material_class`).

### 2. A sourced value

```python
put(
    kind="material",
    id="6061-t6",
    property="density",
    value=2700,
    unit="kg/m3",
    maturity="commercial",
    source="https://matweb.com/...",
)
```

The entity must already exist — create it first. `property=` names a row
in the registry (see below); `value=`/`unit=` are the measurement;
`conditions=` is a free JSONB dict (`{"temper": "T6", "temperature": 298}`)
for whatever axis distinguishes this sample from another of the same
property; `maturity=` is `commercial | lab | speculative` (default `lab`)
— a property of *this measurement*, not of the material.

## The canonical-unit rule

Every property has one canonical unit (or none, for a dimensionless /
categorical / boolean / text property). `unit=` on a value write **must
match it exactly**, or the write is rejected naming the canonical unit —
there is no conversion in v1:

```python
put(kind="material", id="6061-t6", property="density", value=0.0975, unit="lb/in3")
# [error:BadInput] unit='lb/in3' is not density's canonical unit ('kg/m3') -
# v1 is canonical-unit-only, no conversion
```

Convert on your side before writing (or store the value under a
`conditions=` note if you need to keep the original-units reading
somewhere — the canonical field is what `search`'s range filter reads).

## The property registry — core and proposed

`get(kind='material', view='properties')` lists every property: its
canonical unit, dimension, value-type, and tier (`core` — curated, stable
contract — or `proposed` — grown at runtime). The seeded `core` set covers
density, yield/ultimate tensile strength, Young's/shear modulus, Poisson's
ratio, elongation at break, Vickers hardness, thermal conductivity,
specific heat, thermal expansion, melting point, max service temperature
(**temperatures are Kelvin** — absolute scale), electrical resistivity,
dielectric strength, relative permittivity, and cost per mass.

An unknown `property=` on a value write **mints a fresh `proposed`-tier
property** rather than failing, as long as the call gives enough to type
it: pass `unit=` for a numeric property (or omit `unit=` for a
dimensionless one), and `value=` decides the rest — a boolean `value=`
mints a boolean property, a numeric `value=` mints a `quantity`, anything
else mints `text`. A `proposed` property is flagged as such in
`view='properties'` and never silently promotes to `core`.

```python
put(kind="material", id="pfoo-1", property="glass_transition_temp", value=358, unit="K")
# mints glass_transition_temp as a new proposed quantity property (unit=K)
```

Minting a **categorical** property (a closed `allowed_values` set, like the
seeded `crystal_structure` FCC/BCC/HCP example) needs an explicit
`value_type='categorical'` plus `allowed_values=` — inference alone can't
produce one:

```python
put(
    kind="material",
    id="cu-single-crystal",
    property="finish_color",
    value="red",
    value_type="categorical",
    allowed_values=["red", "green", "blue"],
)
```

`value_type=` also overrides inference for `quantity`/`ratio`/`boolean`/
`text` (e.g. force `text` on a numeric-looking code you don't want treated
as a `quantity`). `allowed_values=` is only accepted alongside
`value_type='categorical'`. Passing either against an *already-registered*
property is checked for consistency, not re-minted — a conflicting
`value_type=`/`allowed_values=` is rejected, naming the registered
definition. Writing a categorical *value* validates against
`allowed_values` regardless of how the property was minted: a value
outside the set is rejected, naming the allowed set.

## Recording an uncertainty band

`value_low=`/`value_high=` on a numeric (`quantity`/`ratio`) value record a
range alongside (or instead of) the point value:

```python
put(
    kind="material",
    id="6061-t6",
    property="tensile_strength_yield",
    value=276,
    unit="MPa",
    value_low=270,
    value_high=290,
)
# get/search render this as "276 (270–290)"
```

Omit `value=` and give both bounds to default the recorded value to their
mean — `value_low=270, value_high=290` with no `value=` records `280`.
Giving only one bound with no `value=` is rejected ("give value=, or both
value_low= and value_high="); `value_low=` above `value_high=` is
rejected. A band is numeric-only — `value_low=`/`value_high=` on a
`boolean`/`categorical`/`text` property is rejected, naming the value
type.

## Sourcing a value

A value can carry a source three ways, and `get` shows whichever is
present:

- `source=<a paper or datasheet ref>` (e.g. `source='paper:collins06'`,
  or a bare handle like `source='pa5'`) — optionally with
  `chunk=<chunk address>` pointing at the exact span, same address grammar
  as elsewhere (`slug~5`, a `pc<id>` universal handle, or a bare ordinal).
  The referenced ref must exist.
- `source=<a bare URL>` (e.g. `source='https://matweb.com/...'`) — a plain
  `http(s)://` string is stored as a link, no ref needed (e.g. a datasheet
  you haven't ingested).
- Neither — an unsourced estimate; keep `maturity='speculative'` honest.

```python
put(
    kind="material",
    id="6061-t6",
    property="tensile_strength_yield",
    value=276,
    unit="MPa",
    maturity="commercial",
    source="paper:matweb-alu-2020",
    chunk="matweb-alu-2020~12",
)
```

## Reading it back

```python
get(kind="material", id="6061-t6")  # handbook page, grouped by property
get(kind="material", id="6061-t6", view="table")  # tidy one-row-per-value table
get(kind="material", view="properties")  # the registry
get(kind="material")  # list every material
```

## The range-filter search — the reason this kind exists

```python
search(kind="material", property="thermal_conductivity", max=0.05)
# every material with a recorded thermal_conductivity <= 0.05 W/(m*K),
# each hit carrying the matching value + its source
search(kind="material", property="density", min=2500, max=2800, maturity="commercial")
search(kind="material", q="aluminum")  # name/alias/class lexical match
```

`min=`/`max=` bound inclusively, **in the property's canonical unit** (no
conversion — check `view='properties'` if you're not sure which unit that
is). This is an **interval-overlap** match, not point-in-range: a value
with a `value_low=`/`value_high=` band matches whenever the band overlaps
`[min, max]`, not only when its point falls inside — a value banded
`[270, 290]` matches both `min=285` and `max=275`. A point value (no band)
matches exactly as you'd expect. Omit either bound to leave that side
open. A plain `q=` search matches the material entity's name, aliases, and
`material_class`.

## What's deliberately not here (v1)

- **Unit conversion** — a `pint` helper, convert-on-write, `units=` on
  read. Store and read in the canonical unit; convert on your side.
- **Off-sample estimates / interpolation** — every sample is its own row;
  computing a value *between* samples (or evaluating a published model) is
  a follow-on, not this store.
- **Canonical-value selection** — when sources disagree, `get` shows all of
  them; picking which one to trust is a read-side/human call.
- **Derived properties** (`cost_per_volume` from `cost_per_mass` × `density`,
  insulation R-value from `thermal_conductivity` × thickness) — compute
  these at read time from what's stored; never store a derived number as
  if it were sourced.
