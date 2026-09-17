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

Vendored into precis as its own package: MIT (``LICENSE`` here), never
imports ``precis*`` (``tests/test_hexfold_import_boundary.py``), exported
as its own pip later.  Export seed (README, examples, CITATION) is the
repo-root ``hexfold/`` directory.
"""

__version__ = "0.2.0"
