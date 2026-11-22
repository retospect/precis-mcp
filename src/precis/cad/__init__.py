"""Analytic-IR CAD kernel.

A small, self-contained analytic geometry kernel: rigid-transform-only
primitives (frustum / sphere / torus / half-space-chamfer) that answer
membership, ray-intersection, distance, and face queries in closed form,
plus a boolean DAG fold that keeps subtraction *visible* without ever
computing the merged solid. Edge rounding is a leaf wrapper
(:class:`~precis.cad.primitives.Rounded`: the shape built shrunk, its
exact signed distance offset back out — never a mesh operation) and a
union may ``blend`` with a smooth-min (:mod:`precis.cad.fold`). A
**sampled field** (:class:`~precis.cad.primitives.Field`: a float32 SDF
grid + pitch + origin, trilinear inside its box, the DSL's
``field:<sha256>``) is one more leaf that answers ``distance(p)`` — how
an optimiser's voxel result enters a design — with its grid-side
operations in :mod:`precis.cad.fieldops` (exact Euclidean ``redistance``,
``open``/``close``/``offset``, the SIMP ``from_density`` bridge, and the
payload codec the store keeps a grid in). A design carrying any of the
three exports through the sampled-field backend
(:mod:`precis.cad.fieldmesh`, narrow-band marching cubes over the folded
SDF) while every sharp design still takes the analytic tessellate +
manifold3d route unchanged.

This package deliberately imports **nothing** from the rest of precis
(no DB, no handler, no store) so it stays unit-testable in isolation and
swappable behind the same node-list. The one outward edge is *down* into
:mod:`hexfold.wrap` — the dimension-free cylinder-wrap mapping the
``relief:`` leaf shares with hexfold's nano roll-up (one ``WrapSpec``,
realised as a macro relief here or as lattice sites there;
se-cylinder-wrap-mold slice 4). hexfold is a vendored numpy-only package
that imports no precis, so the isolation holds. That edge is bound
lazily through :mod:`precis.cad._hexfold`: this package is on every
store import path (``precis ingest``/``watch``, the worker), so a
vendored hexfold too old to carry ``wrap`` degrades to a call-time
refusal instead of an import crash (gr524633). The kernel is unit-agnostic
``float64`` throughout — callers pick the length unit; every internal
tolerance is scale-relative (:data:`~precis.cad.vec.LINEAR_REL_EPS`), not
tuned for one magnitude.

The private 3MF writer accepts an opt-in factor on already-millimetre
vertices and records it as namespaced model metadata. Public export APIs
keep their default size: checked SE exports must first prepare and judge
the mesh at printed size before using this prerequisite, since rescaling
after a print check would make its findings describe a different artifact.

That boundary is why sub-assembly instancing (``use <slug> as <name>``)
takes an *injected* ``resolve`` callable rather than reaching for the
store: :func:`~precis.cad.scene.expand_instances` inlines the referenced
design into a flat spec, and the one production resolver lives outside
this package in :mod:`precis.cad_resolve`. Everything downstream — probe,
relate, inspect (:mod:`~precis.cad.inspect`: the ray/section/overlap
inspection family, me691872), export, tessellate —
therefore still sees a plain flat spec and never learns that instancing
exists. A ``field:`` leaf is resolved the
same way — an injected :data:`~precis.cad.dsl.FieldLoader` riding on
:attr:`~precis.cad.scene.SceneSpec.field_loader` (``Store.cad_load``
binds the store's; the grid is never inlined in the DSL).

One-dimensional motion checks live in :mod:`precis.cad.cspace`.
``collision_windows`` walks a signed-gap oracle with conservative
advancement: disjoint material AABBs supply a cheap gap lower bound;
only overlapping boxes call the narrow phase. The sweep handler keeps
the existing ``relate._clearance`` oracle (including its CSG/seed-grid
limitations), so these are resolution-qualified windows, not a new
exact geometric referee. "Never skips" holds relative to the
``_clearance`` reading, which can over-read the true gap; it excludes
sampled contact and sub-resolution excursions. The min-max SDF's factor two is included in
the gap-change bound. Cross-group pairs containing a ``Field`` (including
cutters) refuse: sign-correct grids can have arbitrary gradients, and
even an exact grid's trilinear interpolant need not be 1-Lipschitz. They
need a gradient-aware oracle before conservative advancement is valid.
The motivation is collisions between the old nine
sample states and useful entry/exit coordinates, not lower cost: the
gear-rig regression allows at most 96 exact calls (the former sampler
needed one after gr561417's broad phase).

``scene.expand_instances(joint_frames=...)`` exposes the solver's own
world pivot frames. ``cspace.motion_bounds`` uses AABB-corner radii,
absolute cumulative coupling ratios and both bodies' speeds; fixed
mates and neutral child joints follow their moving anchors. Enclosing
a child's entire relative orbit before measuring its parent's radius
keeps that bound valid for nested articulation. Screw pitch contributes
axial speed. Swept envelopes enclose continuous corner orbits, including
angular extrema; they are not sample-only boxes. Independent joints are
swept separately; cylindrical sweeps hold slide at zero. Overlapping
independent envelopes trigger a simultaneous-motion limitation notice.

The resolution is ``CONTACT_TOL_REL`` times the pair's governing length
at the first state, converted to joint coordinates by the gap-speed
bound. It sets both the advancement floor and sign-change bisection
tolerance. Negative runs require a witness deeper than resolution;
pairs initially in contact get 64 evenly spaced probes including endpoints.
If all stay in band, report joint partners as sampled, not certified:
interference between probes can be missed. Otherwise reuse readings in
the adaptive walk. This bounds a full-turn hinge at 64 exact calls instead
of thousands of resolution steps. Sub-resolution excursions are unresolved.
Unbounded components refuse with a next step because their motion radius
and swept envelope cannot be certified. ``args.n`` is a strict per-pair
exact gap-call budget, including exact calls during bisection
(default 512, about 55 seconds at 108 ms/exact call; maximum 1000000),
not the former sample count. Cheap AABB-only probes have a separate fixed
cap of 20000: clear rigs do not spend the expensive-call budget, but the
walk stays bounded. Exhaustion of either cap refuses with a next step and no
partial verdict. Window coordinates retain precision beyond the usual
three-significant-figure display so the stated edge tolerance survives
rendering. Clearance, connectivity and DOF probes keep their own paths.
"""

from __future__ import annotations

from precis.cad.vec import (
    ANGULAR_EPS,
    LINEAR_REL_EPS,
    Transform,
    deg2rad,
    identity,
    rotation,
    translation,
)

__all__ = [
    "ANGULAR_EPS",
    "LINEAR_REL_EPS",
    "Transform",
    "deg2rad",
    "identity",
    "rotation",
    "translation",
]
