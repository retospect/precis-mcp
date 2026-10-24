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
enters two seams.  ``prism.prism_tube`` (T2's straight-strip variant) closes
a polygonal tube of flat wall strips on signed seams, each seam's third
sheet an outward fin (``+``) or an inward rib (``-``); closure is the
polygon's, not a rail count: net turn 360 (``n_plus - n_minus = 6``), a
zero side-vector sum and a simple cross-section, else named refusals.
``sp3_seam`` extends that pattern to the sp³ seam rows:
``straight_k4`` joins four armchair-rimmed sheets at 90° through one
degree-four seam carbon per period, ``straight_k3z`` joins three
zigzag-rimmed sheets whose seam atoms pair into a chain along the axis
carrying the fourth bond. Rim-type mismatches and off-catalogue
multiplicities refuse before placement; no public grammar is added.

Vendored into precis as its own package: MIT (``LICENSE`` here), never
imports ``precis*`` (``tests/test_hexfold_import_boundary.py``), exported
as its own pip later.  Export seed (README, examples, CITATION) is the
repo-root ``hexfold/`` directory.

``from hexfold import build`` (and ``check``, ``stick``,
``canonical_json``) resolves the verb callables lazily — each name also
names its submodule, so the re-export lives behind PEP 562
``__getattr__`` rather than an eager binding that would shadow it:
``import hexfold.build as m`` keeps yielding the module for the callers
that read (and monkeypatch) module attributes off it, and
``import hexfold`` stays light (``hexfold.stick`` keeps numba out of
the path the same way).  The wart that buys that: the two spellings
share one attribute, so the last import form to touch it wins — a plain
``import hexfold.build`` binds the module back and a later
``from hexfold import build`` in the same process gets the module, not
the callable.  Fresh-process ``from hexfold import …`` — the
``python -c`` / workspace-script path — always gets the callable.
"""

import sys
from typing import Any

__version__ = "0.3.6"

#: verb name -> the submodule that defines the callable of the same
#: name (``canonical_json`` lives in ``hexfold.canon``).
_LAZY_EXPORTS = {
    "build": "hexfold.build",
    "check": "hexfold.check",
    "stick": "hexfold.stick",
    "canonical_json": "hexfold.canon",
}


def __getattr__(name: str) -> Any:
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    fn = getattr(import_module(submodule), name)
    # A first ``hexfold.<verb>`` load binds the submodule onto this
    # attribute; drop the binding again so the next
    # ``from hexfold import <verb>`` still reaches ``__getattr__`` (the
    # submodule itself stays in ``sys.modules`` — ``import
    # hexfold.<verb>`` and ``from hexfold.<verb> import X`` are
    # unaffected).
    sys.modules[__name__].__dict__.pop(name, None)
    return fn
