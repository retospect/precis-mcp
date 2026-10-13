"""hexfold — defect-placement notation for curved sp2 carbon nets.

A ``.hx`` text declares primitives (sheet, tube, cap, cone, C60), lattice-
addressed surgeries (holes, defect glyphs), and connects (``fuse`` k=2,
``bond``, ``seam`` k>=3, nanobud menus); the compiler builds the atom /
bond / ring graph, checks it against the counting law per sheet, and emits
coded findings (``check``), a canonical authored-only JSON with a sha256
content hash (``canon``), the sectioned ``.hx.json`` with a ``generated``
cache block (``build``), and a spring-relaxed stick preview (``stick``,
``xyz``).  Format and semantics live in ``spec.md`` next to this file —
the spec is the truth, the code follows it.

Rim placement tests curved dangling-list winding against the physical
outward normal, signed from the owning instance's centroid. Signing that
normal from the list itself would hide reversed winding and mirror the
seam. ``build._fuse_parity`` reflects compatible seeds before placement;
chiral tubes retain their hand and report ``place.mirror_refused``.
Flat rims have no physical outward side, so their normal instead follows
the winding and the parity constraints of the fuse graph.

``join.compose_k3`` adds the equal-120° sp² straight Y over private open
zigzag segments of resolved planar carbon blocks. Defined endpoints avoid
pretending a finite segment is a cyclic Port; declaration and SD-pattern
refusals precede rigid placement. All copied bond/face indices must be local
integers and faces simple closed input bond walks, so offsets cannot hide
corrupt topology as cross-block faces. It performs no relaxation, public grammar
or SE mutation; a straight deterministic fixture is not curved placement.
``y_junction.straight_y`` constructs finite honeycomb sectors for the SE
scene adapter using that same private admission/placement seam. Two guard
columns close its endpoints; no cyclic Port or unequal solver is invented.
``railtube.rail_tube`` (nanoreactor T2 slice 1) closes a tube of N arcs
meeting at N equal-120 seams along the axis, each seam's third sheet an
inward strip, seams minted in place by ``join._seam_k3`` because every arc
enters two seams.

Vendored into precis as its own package: MIT (``LICENSE`` here), never
imports ``precis*`` (``tests/test_hexfold_import_boundary.py``), exported
as its own pip later.  Export seed (README, examples, CITATION) is the
repo-root ``hexfold/`` directory.
"""

__version__ = "0.3.6"
