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

``join.compose_k3`` adds the equal-120° sp² straight Y over private open
zigzag segments of resolved planar carbon blocks. Defined endpoints avoid
pretending a finite segment is a cyclic Port; declaration and SD-pattern
refusals precede rigid placement. All copied bond/face indices must be local
integers and faces simple closed input bond walks, so offsets cannot hide
corrupt topology as cross-block faces. It performs no relaxation, public grammar
or SE mutation. Closed internal tube rails/curved placement for nanoreactor
T2 remain separate; a straight deterministic fixture is not that capability.

Vendored into precis as its own package: MIT (``LICENSE`` here), never
imports ``precis*`` (``tests/test_hexfold_import_boundary.py``), exported
as its own pip later.  Export seed (README, examples, CITATION) is the
repo-root ``hexfold/`` directory.
"""

__version__ = "0.3.1"
