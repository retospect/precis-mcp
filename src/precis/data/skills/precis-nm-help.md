---
id: precis-nm-help
title: precis — the nm kind (nanomachine block trees over atoms)
summary: hierarchical building-block design for molecular machines — nested blocks with envelopes/poses/ports/connects/L2 threading/DOF and an L5 binding into a real structure design for the filled chemistry; typed ops via put/edit, views tree/block/ports/validate/clearance/topology; dark behind nm.enabled
answers:
  - how do I design a molecular machine as nested blocks before filling in real chemistry?
  - how do I declare a port and connect two blocks with a capability gate?
  - how do I record that a macrocycle is threaded onto an axle?
  - how do I check the envelope clearance between two blocks?
  - how do I bind a block's ports to real atoms in a structure design?
  - why is kind='nm' unavailable or disabled in this build?
applies-to: get/search/put/edit/delete (kind='nm')
status: active
---

# precis-nm-help — nanomachine block trees over atoms

An `nm` design is a **hierarchical block tree**: nested building blocks with
spatial envelopes, poses, ports, and topology — the scaffold you design
*before* filling each envelope with real chemistry. Fourth keystone kind
(glossary: owns a legible IR and rents the heavy kernel only at export; the
LLM traverses a graph, never pixels), sibling to `cad` (solids) / `pcb`
(copper+silicon) / `structure` (atoms). Units are **ångström**, everywhere.
A block is *designed*, not bought (unlike `component`) — the library grows
by composition, a sugar defined once and instanced seven times, the way a
software module tree does.

## Author a design — `put(id=<slug>, text=<JSON>)`

```python
put(
    kind="nm",
    id="rotax1",
    text='''{
  "description": "a rotaxane axle with a threaded crown macrocycle",
  "ops": [
    {"op": "add_block", "name": "axle", "envelope": "cyl:r2h20", "desc": "the threading rod"},
    {"op": "add_block", "name": "hub", "parent": "axle", "envelope": "sphere:r3", "use": "stopper"},
    {"op": "add_block", "name": "rim", "parent": "hub", "pose": [0, 0, 5]}
  ]
}''',
)
```

Payload is JSON: `description?` + `ops` (a list of typed ops, same shape for
`put`/`edit`). `add_block` mints a block: optional `parent` (nests it),
`envelope` (the `cad` mini-DSL string at Å — `cyl:r2h20`, `sphere:r3`,
`box:w2d2h2`, `torus:R5r1`, see `precis-cad-help`), `pose`/`rot` (3-vectors,
Å/deg, default origin), `desc`/`use` (free text, folded into the search
card). Re-`put`ting a slug **replaces** the whole tree (old blocks/ports/
connects/threading soft-retired) — the `structure`/`cad` re-put shape.
`edit(id=<slug>, ops=[...])` applies more ops to the live tree.

## Reuse a block — `instance_block`

```python
edit(kind="nm", id="crown1", ops=[
    {"op": "add_block", "name": "sugar", "envelope": "sphere:r2", "desc": "one sugar unit"},
    {"op": "add_block", "name": "ring_atom", "parent": "sugar"},
    {"op": "instance_block", "name": "sugar2", "template": "sugar", "pose": [5, 0, 0]},
])
```

An instance **resolves** its subtree, envelope, ports, and dof from its
template at read time — never copied. Only `template`/`name`/`parent`/
`pose`/`rot` are accepted; passing `envelope`/`desc`/`use`/`dof` is rejected
(set them on the template instead). An instance can't itself be instanced,
can't nest under its own template's subtree, and an indirect cycle (A
instances B, B instances A) is rejected too — the read-time tree walk would
recurse forever otherwise. `add_port`/`declare_dof`/`bind_structure` on an
instance are all rejected the same way — do them on the template.

## Ports — named attachment points, capability-gated

```python
edit(kind="nm", id="rotax1", ops=[
    {"op": "add_port", "block": "axle", "name": "p1", "roles": ["covalent"],
     "direction": [1, 0, 0], "expected_element": "C"},
])
```

`roles` is a **capability set the caller asserts**, never checked against
real chemistry — see the connect gate below. `direction` (optional)
normalizes to unit length; a zero vector is rejected. Port names may not
contain `.` (the `connect` endpoint syntax reserves it). `remove_port`
refuses while a live connect or a declared dof still references the port.

## Connect two ports — the capability gate

```python
edit(kind="nm", id="rotax1", ops=[
    {"op": "connect", "a": "axle.p1", "b": "hub.p1"},
    # kind='bond' (default) needs both ports to afford 'covalent'
    {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "interaction",
     "objectives": {"role": "pi_stack"}},
])
```

Endpoints are `'block.port'` (split on the last dot). A `kind='bond'`
connect requires **both** ports to afford `'covalent'` — or, with
`objectives={'role': ...}`, both must afford that named role instead.
Rejection names the port's actual roles. This is a **declared-intent
check**: it catches a connect that contradicts the caller's own `add_port`
labels, not one that's chemically implausible — `roles` are never
independently verified against real chemistry. `kind='interaction'`
(non-bonded) skips the gate entirely. `disconnect` removes a live connect by
its endpoint pair.

## Threading — a topology invariant, stored not derived

```python
edit(kind="nm", id="rotax1", ops=[
    {"op": "declare_threading", "a": "rim", "b": "axle"},  # 'rim' threaded through 'axle'
])
```

Directional, per-pair. A rotaxane's mechanical interlock is a **topological
fact**, not a geometric one — it is stored explicitly and never re-derived
from block poses (the same rule `pcb` uses for its combinatorial embedding:
stored, not recomputed from coordinates). Mutual threading (`a` through `b`
*and* `b` through `a`) is rejected as physically impossible — each would be
inside the other. Fix a wrong-direction declaration with `remove_threading`,
not by declaring the opposite pair on top.

## Declared DOF — a block's degree of freedom

```python
edit(kind="nm", id="rotax1", ops=[
    {"op": "add_port", "block": "axle", "name": "pA"},
    {"op": "add_port", "block": "axle", "name": "pB"},
    {"op": "declare_dof", "block": "axle", "kind": "rotational", "axis_ports": ["pA", "pB"]},
])
```

`kind` is `'rotational'` or `'translational'`; `axis_ports` names exactly
two of the block's **own** ports. `clear_dof` removes it. `declare_dof`
only *records* the axis — it computes nothing about it (no torsion scan, no
barrier estimate; see Scope below).

## Bind a block to real chemistry — `bind_structure`

```python
put(kind="nm", id="bind1", text='''{"ops": [
    {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
    {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
    {"op": "bind_structure", "block": "hub", "design": "frag1", "ports": {"p1": "aC1"}}
]}''')
```

Maps a block's ports to atoms in a real `structure` design
(`precis-structure-help`) — the L5 fill. Every mapped port must exist on the
block, the atom label must exist in the structure, and when a port declares
`expected_element` it must match the bound atom's actual element — a loud
rejection at bind time, never a silent mismatch. Binding again to the
**same** design is incremental (an earlier call's port map survives);
binding to a **different** design first clears every port binding on the
block (the old design's atom labels mean nothing in the new one — leaving
them would strand a `bound_atom` pointing at the wrong atom, or read as a
false `dangling_binding`). `unbind_structure` clears a block's binding and
every one of its ports'. Both only ever target an ordinary block — bind via
the template for an instance.

## Remove ops — the guard behaviors

`remove_block` refuses while any live block elsewhere instances it (or a
descendant) — remove the instance first. Past that guard, it silently drops
any connect or threading pair touching the removed subtree (the `structure`
vacancy precedent: removing an atom drops its bonds too) — `validate`'s
`dangling_connect`/`dangling_threading` exist to catch a case where this
somehow doesn't run.

## Read the design — `get`

```python
get(kind="nm")                                                   # list designs
get(kind="nm", id="rotax1")                                       # view='tree' (default) — nested TOC
get(kind="nm", id="rotax1", view="block", args={"name": "hub"})    # one block: pose, envelope, ports, connects, threading
get(kind="nm", id="rotax1", view="ports")                          # every block's live ports in one table
get(kind="nm", id="rotax1", view="topology")                        # every threading pair + declared dof
get(kind="nm", id="rotax1", view="validate")                         # feasibility findings, two tiers
get(kind="nm", id="rotax1", view="clearance", args={"a": "axle", "b": "hub"})  # signed envelope gap
```

The tree view marks each line with an inherited-envelope note (`(from
tmpl)`) on an instance, a `[N port(s)]` suffix, a `[rot]`/`[trans]` dof
marker, and `⇒ st:<slug>` when a block is bound to a structure design.

### `view='clearance'` — signed envelope gap

Builds a fresh `cad` design in memory from each block's own envelope (an
instance uses its template's), placed at the block's pose+rot, and runs the
same exact-sign CSG SDF `cad`'s `view='clearance'` uses: **positive** =
clear, **≈0** = touching, **negative** = interference. Because envelope
primitives are exact-sign
SDFs, not bounding boxes, a `torus:` (ring) block's inner bore genuinely
reads as empty — an axle block threaded through a ring block's hole doesn't
falsely collide against the ring's outer silhouette. **A block's envelope is
its own only** — a child's envelope is never unioned into its parent's for
this check, so the response notes it when either queried block has children
that themselves declare an envelope (a later increment, not modeled today).

### `view='validate'` — every rule

| rule | severity | catches |
|---|---|---|
| `dangling_connect` | error | connect names a block/port that no longer exists |
| `port_capability` | error | a stored connect violates its own endpoints' declared roles |
| `dangling_threading` | error | threading names a block that no longer exists |
| `dangling_binding` | error | `bound_design` no longer resolves, or a `bound_atom` no longer exists in it |
| `unconnected_port` | warn | a declared port with no live connect — normal mid-design |
| `blocks_without_envelope` | warn | a block with ports but no envelope — geometry needed before L1 |
| `threaded_without_envelope` | warn | a threading pair where either side has no envelope — the interlock can't be verified geometrically yet |
| `binding_element_mismatch` | warn | a bound atom's element doesn't match the port's `expected_element` |

Zero findings is trivially achievable by declaring nothing — `validate`
checks what you *did* declare, not completeness.

## Find a design — `search`

```python
search(kind="nm", q="rotaxane axle")
```

One embeddable summary card per design (title + description + every
block's `desc`/`use` + port names/roles), so a design described only by its
ports' capability vocabulary (`'covalent'`, `'coordination'`) is still
findable by that. Joins the cross-kind fan-out `search(kind='*', q='...')`.

## Retire a design

```python
delete(kind="nm", id="rotax1")  # soft-retire the ref + every live block/port/connect/threading row
```

## Dark by default — `nm.enabled`

`nm` ships gated behind the `nm.enabled` setting (`PRECIS_NM_ENABLED` env
fallback; see `precis-settings-help`) — unset means the kind doesn't
register at all. A call against it before the operator turns it on raises
`Unsupported` naming the missing setting; see `precis-kinds-disabled-help`.

## Scope limits — stated plainly

- A block's clearance envelope is its own config only — no subtree union
  across children (see `view='clearance'` above).
- **No fill loop.** Going from a declared port to a real chemical fragment
  is manual: mint or find a `structure` design yourself, then
  `bind_structure` it in. There is no lit-search-and-attach automation yet.
- **No charge, optical, or simulation views.** No mechanism/dynamics
  analysis, no torsion scan, no rotational-barrier estimate —
  `declare_dof` records intent only.
- Port `roles` are declared-intent labels, never chemistry-checked (see the
  connect capability gate above) — a role-consistent connect is not a claim
  the bond is real chemistry.

## See also

```python
get(kind="skill", id="precis-structure-help")  # the atom side — bind_structure's target, relax, DFT ladder
get(kind="skill", id="precis-cad-help")  # the envelope mini-DSL and the clearance kernel nm reuses
get(kind="skill", id="precis-settings-help")  # requires_setting gating, how an operator enables a dark kind
```
