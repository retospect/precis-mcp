---
id: precis-component-help
title: precis — the component kind (general procurable-part store)
summary: bolts/hoses/pipes/bearings/... with per-value sources — entity vs value writes, category + spec registries, the canonical-unit rule, made_of, and the range-filter search
applies-to: get/put/search (kind='component')
status: active
---

# precis-component-help — sourced component specs, canonical units only

`component` is a general store for discrete **procurable things** — bolts,
hoses, pipes, beams, gaskets, bearings, adhesives, electronic components.
It's a `material`-shaped star schema (entity + typed registry + value fact
table, canonical-units-only) plus one added dimension: a **category**
(`fastener`, `hose`, `pipe`, ...). Specs are optionally scoped to a
category — `unit_cost`/`mass`/`length_overall` are universal, but
`bore_diameter` only applies to a `hose`.

This is deliberately **not** `part` — `part` is the JLCPCB/LCSC catalog,
ingest-only, addressed by C-number, SKU reference data. `component` is the
`put`-driven engineering store above it: you can record a hydraulic hose or
a structural beam here, neither of which `part` can hold.

## Two writes share one `put`

`put(kind='component', ...)` does one of two things, discriminated by
whether `spec=` is present.

### 1. The entity (name / category / mpn / manufacturer)

```python
put(
    kind="component",
    id="m6-a2-bolt",
    title="M6x20 A2 socket cap",
    category="fastener",
    uom="each",
    meta={"mpn": "SCS-M6-20-A2", "manufacturer": "Acme Fasteners"},
)
```

`id=` is a slug you pick. `category=` is **required** the first time you
create the entity — it must name a row in the category registry
(`get(kind='component', view='categories')`); an unknown category **mints
a fresh `proposed` category** rather than failing, same discipline as
`material`'s proposed properties. `uom=` is the unit of measure the
component is counted/priced in (`each | m | kg | L | m2 | ...`).

Calling `put` again on the same `id=` **updates** the entity: `title=`
overwrites if given, `meta=` shallow-merges (adding an alias doesn't clobber
an already-recorded `mpn`).

### 2. A sourced value

```python
put(
    kind="component",
    id="m6-a2-bolt",
    spec="thread_pitch",
    value=1.0,
    unit="mm",
    source="datasheet:acme-scs-catalog",
)
```

The entity must already exist — create it first. `spec=` names a row in
the spec registry (see below); `value=`/`unit=` are the measurement;
`conditions=` is a free JSONB dict for whatever axis distinguishes this
sample from another of the same spec; `maturity=` is `commercial | lab |
speculative` (default `lab`) — a property of *this measurement*, not of
the component. `as_of=` (`'YYYY-MM-DD'`) dates the measurement — load-bearing
for `unit_cost`.

### 3. made_of — what it's made of

```python
put(kind="component", id="m6-a2-bolt", made_of="material:6061-t6")
```

`made_of=` rides on any `put(id=...)` call, independent of whether that
call is also an entity or value write — pass it alongside a `title=`/
`category=` entity put, alongside a `spec=` value put, or on its own. It
must resolve to a `material` ref (rejected, naming the resolved kind,
otherwise) and creates a `made-of` link, visible on `get(id=...)`. This is
the one composition edge v1 ships — it records the substance a component
is made of; nothing yet *computes* the component's intensive properties
(density, modulus, ...) from the linked material — that's a deferred
follow-on (see below).

## The category registry — core and proposed

`get(kind='component', view='categories')` lists every category and its
tier. The seeded `core` set: `fastener`, `hose`, `pipe`, `profile`,
`electronic`, `adhesive`, `seal`, `bearing`, `fitting`, `laminate`. It's
**flat** in v1 — no parent/child taxonomy.

## The spec registry — universal, category-scoped, core and proposed

`get(kind='component', view='specs')` without `id=` lists the whole
registry (like `material`'s `view='properties'`); pass `id=` for a specific
component to narrow it to what's *applicable to that component's
category* — universal specs (`category_id` NULL) plus that category's own:

```python
get(kind="component", view="specs")  # the whole registry
get(kind="component", id="m6-a2-bolt", view="specs")  # universal + fastener specs
```

Universal specs (curated `core`): `mass` (kg), `unit_cost` (USD),
`length_overall` (m). **Per-unit cost is just the universal `unit_cost`
spec** — no special-cased cost column:

```python
put(
    kind="component",
    id="m6-a2-bolt",
    spec="unit_cost",
    value=0.12,
    unit="USD",
    as_of="2026-07-01",
    conditions={"qty_break": 100},
)
```

Category-scoped `core` examples: fastener → `thread_size` (categorical),
`thread_pitch` (mm), `length` (mm), `grade` (categorical), `drive_type`
(categorical); hose → `bore_diameter` (mm), `max_working_pressure` (MPa),
`min_bend_radius` (mm), `temperature_max` (K, **absolute scale**); bearing
→ `bore_diameter_bearing` (mm), `dynamic_load_rating` (N).

A value write for `spec=S` on a component in category `C` is accepted only
if `S` is universal or `S`'s category is `C` — a spec that doesn't apply to
this component's category is rejected, naming the category:

```python
put(kind="component", id="m6-a2-bolt", spec="bore_diameter", value=10, unit="mm")
# [error:BadInput] spec='bore_diameter' applies to category 'hose', not 'fastener'
```

An unknown numeric `spec=` **mints a fresh `proposed` spec scoped to that
component's category** (not universal — a proposed hose spec never leaks
into fasteners):

```python
put(kind="component", id="some-hose", spec="burst_pressure", value=60, unit="MPa")
# mints burst_pressure as a new proposed quantity spec, scoped to category='hose'
```

Minting a **categorical** spec (a closed `allowed_values` set) is not
exposed at write time in v1 — categorical specs are curated additions to
the registry, same as `material`.

## The canonical-unit rule

Every spec has one canonical unit (or none, for a dimensionless /
categorical / boolean / text spec). `unit=` on a value write **must match
it exactly**, or the write is rejected naming the canonical one — no
conversion in v1:

```python
put(kind="component", id="m6-a2-bolt", spec="thread_pitch", value=0.04, unit="in")
# [error:BadInput] unit='in' is not thread_pitch's canonical unit ('mm') -
# v1 is canonical-unit-only, no conversion
```

## Sourcing a value

Identical to `material`'s provenance rule: a value can carry a source
three ways —`source=<a paper or datasheet ref>` (optionally with `chunk=`
pointing at the exact span; a `chunk=` from a different ref than `source=`
is rejected), `source=<a bare http(s) URL>`, or neither (an unsourced
estimate; keep `maturity='speculative'` honest).

```python
put(
    kind="component",
    id="m6-a2-bolt",
    spec="grade",
    value="A2",
    source="datasheet:acme-scs-catalog",
    chunk="acme-scs-catalog~4",
)
```

## Reading it back

```python
get(kind="component", id="m6-a2-bolt")  # component page, grouped by spec
get(kind="component", id="m6-a2-bolt", view="table")  # tidy one-row-per-value table
get(kind="component", view="specs")  # universal specs
get(
    kind="component", id="m6-a2-bolt", view="specs"
)  # + this component's category specs
get(kind="component", view="categories")  # the category registry
get(kind="component")  # list every component
```

The component page also shows the `made-of` material, if linked.

## The range-filter search — the reason this kind exists

```python
search(kind="component", spec="max_working_pressure", min=20, category="hose")
# every hose with a recorded max_working_pressure >= 20 MPa, each hit
# carrying the matching value + its source
search(kind="component", spec="unit_cost", max=0.5)
search(kind="component", q="M6")  # name/mpn/manufacturer/category lexical match
```

`min=`/`max=` bound `value_num` inclusively, **in the spec's canonical
unit**. `category=` optionally narrows the search to components in that
category (useful when a spec_id collision is theoretically possible across
categories, or just to keep results on-topic). A plain `q=` search matches
the component entity's name, aliases, mpn, manufacturer, and category.

## What's deliberately not here (v1)

- **BOM / assemblies** — a `contains → component` edge with quantity, and
  recursive cost/mass rollup over the assembly tree. v1 stores flat
  components and their `made-of` material only.
- **Laminate layer structure** — ordered layers and effective-property
  computation from the stack. v1 admits a `laminate` category (record its
  measured specs), but not the structured layer model.
- **Effective-property inheritance** — computing a component's intensive
  properties from its `made-of` material at read time. v1 records the
  edge; it does not walk it to synthesize values.
- **`realized_by → part` binding** — auto-linking a component to a
  concrete JLCPCB C-number and pulling its live price/stock. `component`
  stays independent of the catalog kind.
- **Category taxonomy tree** — parent/child categories, inherited spec
  sets. v1 categories are flat.
- **Unit conversion** and **off-sample estimation** — same v1 trims as
  `material`; convert on your side before writing.
- **Runtime categorical/uncertainty writes** — runtime spec-mint is
  numeric/boolean/text-only (categoricals seed via migration), and
  `value_low`/`value_high` columns exist but have no write path yet.
