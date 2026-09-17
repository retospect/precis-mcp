---
id: precis-cad-assembly-help
title: precis — assemble CAD designs (ports, mates, joints, connectivity)
summary: connect cad sub-assemblies by named port and computed mate instead of world coordinates, add articulated joints and printed-in-place hinges, and verify the result by clearance/interference/DOF and post-cut connectivity
answers:
  - how do I check clearance or interference between two parts?
  - how do I connect two designs by port and mate instead of typing world coordinates?
  - how do I add a hinge or slide joint between two parts of an assembly?
  - is this assembly one connected solid, or does it have floating bodies?
applies-to: get/put(kind='cad') — ports, mates, joints, connectivity
status: active
tags: [design]
kinds: [cad]
---

# precis-cad-assembly-help — connect CAD parts by interface, not coordinates

A design instances other designs (`use <slug> as <name>`, see
[[precis-cad-help]]); this skill covers wiring those instances together —
by named port and computed mate, by articulated joint, and by verifying
the result is one physically connected solid.

## Author a design — assembly and payloads

### Assemble by interface — `port` and `mate`

Typing world coordinates for every sub-assembly is where designs (and
models) drift. Declare a **port** — a named frame on a design — and let a
**mate** compute the pose:

```python
put(kind="cad", id="nema17", text="""
component body
case   add  box:w42mmd42mmh40mm
port   shaft  @0mm,0mm,40mm     # the output face, 40 mm up its own z
""")

put(kind="cad", id="drivetrain", text="""
port deck @0mm,0mm,12mm          # a frame on THIS design

use gearbox as g
use nema17  as m

mate g.input to deck             # anchor = one of this design's ports
mate m.shaft to g.output flip    # anchor = another instance's port
""")
```

- `port <name> [@x,y,z] [rot:rx,ry,rz]` is a **top-level directive** like
  `component` / `use` — it names a frame, not geometry, so it never becomes
  a node, never appears in a probe, and never exports.
- `mate <instance>.<port> to <anchor> [flip] [spin:<angle>]` places
  `<instance>`. The anchor is either `<port>` (this design's own, fixed) or
  `<instance>.<port>` (another instance, posed first).
- **The default is coincidence** — the two frames land exactly on top of
  each other, same origin, same axes. That is "put your connection point
  right here". `flip` adds an explicit 180° about x (the two faces then
  oppose, which is what you want for a shaft entering a bore); `spin:<angle>`
  (`deg`/`rad`, e.g. `spin:30deg`) rotates about the port's z, for clocking
  a bolt pattern.
- Addressing is **one level**: `m.shaft`, not `m.inner.shaft`.
- An instance with no mate sits where you placed it (the origin by default)
  — a frame or base part needs no mate.

Refused at `put`, each naming the offender: mating an instance twice, or
mating one that also carries `@`/`rot:` (both over-constrained — a mate
already fixes all six DOF); mating a `polar:`/`linear:` instance; a mate
cycle (`a` mated to `b` mated to `a`); an unknown instance or port (the
error lists the ports the design actually declares).

Ports are also **searchable**: they go into the design's one card, so
`search(kind='cad', q='nema17 mount port')` finds designs by the interfaces
they advertise.

Ports take two optional tags: `type:<t>` (free compatibility tag — two
*typed* ports only mate when the types match, an untyped side always may)
and `of:<component>` (scopes the frame to a component — required for the
pivot of a component `joint` below).

### Straddling modules — `payload … at:<port>`

A port may carry **payload geometry** — features the module machines into
whatever it mates against (a hinge's knuckle recess, its pin bore):

```
component body
barrel  add cyl:r4mmh20mm
port leaf_a @-10mm,0mm,0mm of:body type:hinge-leaf
payload recess   cut box:w8mmd3mmh20mm at:leaf_a @0mm,0mm,-10mm
payload pin_bore cut cyl:r2mmh24mm     at:leaf_a @0mm,0mm,-2mm
```

`payload <name> <op> <config> at:<port> [@x,y,z] [rot:...]` — placement is
relative to the port's frame; `op` is `add`/`cut` only. On the module
itself the payload is dormant. When the port **mates**, each payload is
spliced into the component on the *other* side as a node named
`<instance>~<name>` — so the host's tree and `view='volume'` (which adds a
"payload contribution" delta line) attribute the change to the module,
never silently. Requirements and refusals: the far side's port must be
scoped `of:` a component (the host body — refused otherwise, a payload
needs a host); across an articulated `joint …` the payload stays rigid in
the host (a recess doesn't swing with the hinge). An instanced module
whose payload port is never mated is flagged at `put`
(`⚠ payload port(s) never mated`) — the geometry would exist in no host.

## Author a design — joints and print-in-place

### Articulate — `joint`, `state`, and `view='sweep'`

A **mate is a `fixed` joint**. The articulated kinds insert one degree of
freedom at the interface, about/along the **anchor frame's z axis**:

```python
put(kind="cad", id="crane", text="""
component tower
mast add box:w20mmd20mmh200mm

component jib
beam add box:w150mmd10mmh10mm @75mm,0mm,205mm
port slew @0mm,0mm,205mm of:jib

joint jib revolute at:slew limits:-170deg..170deg     # component form

use hook_block as h
joint h.eye to jib.tip prismatic limits:0mm..180mm    # instance form
""")

# pose it — a joint's name is its subject instance / component; every
# probe arg that's a length or angle carries its own explicit unit too,
# same rule as the source text:
get(kind="cad", id="crane", view="point",
    args={"state": {"jib": "45deg", "h": "120mm"}, "p": ["0mm", "90mm", "205mm"]})

# the payoff question — does anything hit anything, anywhere in the travel?
get(kind="cad", id="crane", view="sweep")
```

- Kinds: `revolute` (deg) · `prismatic` (mm) · `cylindrical`
  (`[deg, mm]`, two DOF) · `screw` (deg, advances `pitch:<length>` per
  rev) · `fixed` (= `mate`). `limits:`/`pitch:` at the text boundary always
  carry an explicit unit; internally revolute/screw/cylindrical-angle state
  is radians, prismatic/cylindrical-slide state is metres.
- **Two forms**: `joint <inst>.<port> to <anchor> <kind> [opts]` poses an
  instance (a generalised mate — `flip`/`spin:` still apply);
  `joint <component> <kind> at:<port>` articulates a whole component of
  *this* design about a port scoped `of:` that component.
- `state=` in any probe/export view's `args` poses the design. Missing
  joints default to 0 (clamped into `limits:`); an **explicit** state
  outside `limits:` is an error, never clamped. `state` addresses only the
  top design's joints — instanced sub-designs pose at their defaults.
- `gear <a> to <b> ratio:<r>` / `belt …` couple two joint states
  (`b = r × a`; the sign carries the sense, so contact gears want a
  negative ratio). A driven joint derives; setting it explicitly to a
  conflicting value is an error.
- A mate/joint anchored on a port `of:` a jointed component **follows**
  that component — a motor mated onto an articulated arm swings with it.
- `view='sweep'` sweeps each joint across its `limits:` (others held
  neutral, `args.n` samples, default 9), reporting every colliding pair
  with the state range where it interferes, plus the swept envelope per
  moving body. `args={'joint': 'jib'}` sweeps one joint only.

### Print-in-place joints — the `printed-` type convention

For 3D-printed realizations, hinges/slides/pins can be **built in** —
printed captive, no assembly. Express one as a module: its own pin as a
node reaching into the host, the bore as a `cut` payload (pin radius +
process clearance), the `joint … revolute|prismatic` line, and a port
`type:printed-hinge` (the `printed-` prefix marks the interface as
captive-printed — "pip" in 3D-printing parlance, spelled out here to
avoid the Python-pip collision). Put the clearance floor in a dim
(`dim clearance >= 0.3mm` for FDM) so an undersized joint refuses at
parse. The honesty rule rides make-tree alignment: a `printed-` mate
whose two hosts are `made-by` **different print steps** is flagged on
the design's `view='links'` — a captive joint needs both sides in the
same print.

## Relate parts — clearance / interference / DOF

Built at real dimensions and *analyzed*, not declared (there is no `fit`
object — a press fit is simply *clearance = −0.02 mm*, and whether that's
intended is your call):

```python
# signed min gap between two components: + clear, ≈0 line-to-line, − interference
get(kind="cad", id="asm", view="clearance", args={"a": "shaft", "b": "hub"})

# how far one part can translate along ±x/±y/±z before hitting another
get(kind="cad", id="asm", view="dof", args={"moving": "shaft", "fixed": "hub"})
```

Clearance is measured against the *material* — a shaft sitting in a bored
hub reads the **radial wall gap**, not a false collision against the
un-bored plate.

## Connectivity — is it one solid? what touches what?

`view='connectivity'` builds the **contact graph** over the design's
components: two parts are *connected* when their realised (post-cut)
material touches or overlaps (signed gap ≤ tol). It answers three questions:

```python
# full report: the connected bodies + every contact + the one-solid verdict
get(kind="cad", id="wheel", view="connectivity")

# what touches this part? (empty ⇒ a floating body)
get(kind="cad", id="wheel", view="connectivity", args={"of": "hub"})

# is there a contact path between two parts? (e.g. hub → rim through spokes)
get(kind="cad", id="wheel", view="connectivity", args={"a": "hub", "b": "rim"})

# loosen/tighten what counts as "touching" (explicit unit; default is
# scale-relative to the design's own bbox diagonal, not a fixed mm figure)
get(kind="cad", id="wheel", view="connectivity", args={"tol": "0.05mm"})
```

Because contact is tested on the **folded CSG** (cuts already applied),
the classic trap is avoided: a rim (`disc − cutout`) and a hub
(`disc − cutout`) whose *raw* discs overlapped massively before the cuts
are correctly seen as **not touching** — only their post-cut annulus/disc
material counts. So "is the hub connected to the rim?" gives the physical
answer, not the pre-cut one.

### Truisms — a real part is one connected solid

A manufacturable part is a *single connected body*: a wheel is its hub, its
spokes, **and** its rim, and they must all touch (directly or through each
other). Model each distinct body as its own **component** (`hub`, `rim`,
`spoke`) — then `connectivity` verifies the whole thing hangs together, and
`put` warns you at author time if it doesn't:

- `⚠ floating (touches nothing): rim` — a part welded to nothing.
- `⚠ 2 disconnected bodies: hub+spoke | rim` — two islands that should be one.

After any edit that moves or resizes a body, re-check connectivity: a spoke
nudged 0.1 mm too short silently disconnects the rim. Connectivity is at
the **component** level — a stray *instance* inside one component isn't
caught; keep distinct bodies as distinct components.

One cost caveat: every `put` runs a pairwise clearance/interference sweep
over all components — O(N²) in **component count**, and a ~14-component
assembly can push a `put` past 120 s. For larger assemblies either merge
bodies you don't need connectivity verdicts on, or expect to background
the `put` and poll.

## See also

- [[precis-cad-help]] — base authoring model: node lines, dims, config DSL, probes
- [[precis-cad-build-help]] — make-tree, mass, BOM, and print orientation
