---
id: precis-cad-build-help
title: precis — plan and weigh a CAD build (make-tree, dimensions, mass, BOM)
summary: plan a design's build order, catch dimension contradictions before geometry exists, weigh it from sourced material densities, roll up a BOM against catalog parts, and pick a print orientation
answers:
  - how do I plan the order a design gets built or assembled?
  - how do I compute mass and center of mass from real materials?
  - how do I get a BOM with sourced catalog parts (bearings, bolts, extrusion)?
applies-to: get/put(kind='cad'), get/put(kind='make') — build planning, dimensions, mass, BOM
status: active
tags: [design]
kinds: [cad]
---

# precis-cad-build-help — plan, weigh, and source a CAD build

Once a design exists (see [[precis-cad-help]]), this skill covers turning
it into something buildable: an ordered make-tree, driving dimensions the
kernel checks for contradictions, a materials-based mass rollup, a sourced
BOM, and a print orientation.

## Author a design — build planning, dimensions, mass

### Plan how it's built — `kind='make'` + `rel='made-by'`

A design tree says what a thing IS; a **make-tree** (`kind='make'`) says
the ORDER it comes together — and the two need not align (a step may
bundle parts across subsystems). Steps are first-class, ordered nodes
addressed `mk<id>`, each carrying its conditions in `meta`:

```python
put(kind="make", id="crane-assembly", title="crane assembly order")
put(kind="make", id="crane-assembly", text="bolt tower to base",
    meta={"fixture": "torque wrench", "torque": "40 Nm"})
# align blocks from the design side — many-to-many, ref- or step-level:
link(kind="cad", id="crane", target="make:crane-assembly", rel="made-by")
link(kind="cad", id="tower_sub", target="mk123", rel="made-by")
```

`get(kind='make', id=…)` renders the step tree with each step's
conditions and its aligned blocks (`⛓`). Once a design declares a
make-tree, `view='links'` on the design warns about `contains`
sub-designs not aligned to any step (`⚠ make-coverage`). Steps track
`status=open|wip|done`; `edit` moves/rewords a step without changing its
handle. Two make-orders over the same design (placed assembly vs bulk
synthesis) are just two `make` refs.

### Declare dimensions — `dim` / `constrain` (refuse the impossible)

Name your driving dimensions and let the kernel catch contradictions
**before any geometry exists**:

```
dim a = 200mm          # exact
dim c >= 100mm         # one-sided bounds are first-class ("longer than
dim c <= 500mm         # 10cm" is a valid open-ended requirement)
constrain a = c        # equality between dims
```

Bounds on one name intersect; `constrain` merges dims into an equality
class; a class whose combined range is empty — `a = 200mm`, `b = 150mm`,
`constrain a = b` — is **refused at put** with the members and their
bounds named. They're the carrier for process rules like print
clearances (`clearance >= 0.3mm` for FDM) and for estimates that narrow
over time.

**Configs can reference dims**: `slab add box:w{a}d{b}h0.01` — the stored
source stays parametric (edit the `dim` line, geometry follows). Once a
config references `{name}` at all, every literal number in *that same
config token* parses bare/canonical (SI metres) — so the un-substituted
`h0.01` above is 0.01 metres, not millimetres, exactly like the dim values
`{a}`/`{b}` it sits next to (a `dim` declaration is unit-required and
converts to SI once, at the `dim` line, so the two agree in scale). A
referenced dim must be **pinned** to an exact value (directly or through
its equality class); a still-open bound is refused, never silently
averaged. Sub-designs resolve `{…}` against their own dims; payload
configs may not reference dims.

### Weigh it — `material <component> <slug>` + `view='mass'`

Assign each component a `material` kind slug; the mass view joins that
material's **sourced** density (canonical kg/m3) against sampled
per-component volume:

```
component frame
slab add box:w100mmd100mmh10mm
material frame 6061-t6
```

`get(view='mass')` → per-component table (volume ±err, density, mass,
**source** — the numbers arrive cited), total ± sampled-volume error,
CoM. Components without a material are listed as excluded, loudly —
never silently zeroed. Sub-designs bring their own assignments in
(namespaced), and `state=` poses the design first, so CoM at a joint
state is one call.

## Catalog parts — `part <name> <family>:<code>` → `view='bom'`

Standard procurable parts are built in — envelope + ports, never true
thread/ball geometry (what matters is honest outer shape, mate frames,
and procurement identity):

```
part b1 bearing:6202            # d15 D35 B11; ports: bore (midplane), face
part bolts bolt:m6x20 @40mm,0mm,10mm polar:n4r30mm   # patterns multiply BOM qty
part m1 nema:17                 # ports: face (mount plane), shaft
mate b1.face to seat            # parts mate like instances — no coordinates
```

(`bolt:m6x20`'s `m6x20` is the part's designation — mm by fastener-standard
convention, a procurement code rather than a free quantity, so it does
NOT take a unit token; only the placement tokens `@`/`polar:` do.)

Families: `bearing:6202` (deep-groove, 60x/62xx/63xx), `bolt:m6x20` /
`nut:m6` / `washer:m6` (ISO 4017/4032/7089, M3–M12), `extrusion:2020x400`
and `rail:mgn12x200` (profile × cut length), `nema:17` (11–34),
`gear:m1z20[w8]` (blank, OD = m·(z+2)). Unknown codes refuse at `put`
naming what IS known. Parts-only designs need no sub-design resolution.

`view='bom'` flattens the whole assembly (patterns × nesting) to one row
per distinct code and resolves each to the procurable `component` ref
under the catalog's slug (`bearing-6202`, `bolt-m6x20`, length-free for
cut stock) with its recorded `unit_cost`. No matching component ref =
listed `⚠ unsourced` — seed one under that slug to price it. Each save
syncs `realized-by` links design→component for resolved parts;
hand-name extra candidates with
`link(kind='cad', id=…, target='component:<slug>', rel='realized-by')`
(never pruned by the sync). Fabricated bodies are make-tree territory,
not BOM lines. **`se` designs emit the same edge** from their
`set_binding` bindings, so asking a component what calls for it
(`rel='realizes'`) reaches both tracks in one query; each sync prunes
only its own managed rows.

## Print orientation — `view='printability'`

The one probe that meshes (`manifold3d`, the export kernel): searches
build-down directions for the one that prints best. se's fdm
implementer (`precis-se-print-help`) shares it:

```python
get(kind="cad", id="bracket", view="printability",
    args={"max_overhang": "50deg", "max_bridge": "8mm",
          "layer_height": "0.2mm", "min_bed_contact": 0.15})
```

Scores overhang area, bed contact, height, and bridge spans with flat
weights (se passes its own; loads are se's lane). An omitted `args`
field (`max_overhang`, `max_bridge`, `layer_height`, `min_bed_contact`,
`sweep_deg` default 30°) skips that term, never guesses a threshold, and
the reply says so. Pin `args.down=[x,y,z]` to check one orientation; the
reply names any better candidate. Returns the top 5 candidates plus
process-DRC findings (overhang, bridge, bed_contact, build_volume).

> **Tip — need a number, exactly?** Don't eyeball arithmetic. The
> `calc` kind is a local sympy engine: `get(kind='calc', q='2+3*4')`
> evaluates arbitrarily complex expressions *exactly* — fractions,
> roots (`sqrt(2)`, `2**10`), **trig** (`sin cos tan atan2`, `pi`), even
> calculus and linear algebra. Handy here for bolt-circle coordinates,
> slant/draft angles, and tolerance stacks before you `put` them into
> the source.
> **`calc` trig is in degrees by default** — matching cad's convention —
> so `get(kind='calc', q='sin(30)')` → `1/2` and `get(kind='calc',
> q='N(atan2(1,1))')` → `45` directly, and the result carries a
> "degrees" note. Pass `view='rad'` for radians (symbolic calculus);
> wrap in `N(...)` for a decimal instead of the exact form.

## See also

- [[precis-cad-help]] — base authoring model: node lines, dims, config DSL, probes
- [[precis-cad-assembly-help]] — ports, mates, joints, connectivity
- [[precis-material-help]] — the `material` kind `material <component> <slug>` assigns
- [[precis-component-help]] — the catalog `component` refs a BOM resolves against
- [[precis-differentiation-help]] — pick the derivative route before descending on a shape or sizing objective
