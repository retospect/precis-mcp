"""``viz3d`` — a pure, domain-neutral 3D → SVG render core.

No store, no I/O, no web, no model calls: this package turns a scene of
plain geometric primitives (:mod:`.primitives`) plus a camera
(:mod:`.camera`) into an SVG string (:mod:`.render`). It knows nothing
about ``se``/``cad``/``structure`` — the *one* domain-specific renderer in
this slice, the CPK stick-figure (:mod:`.stickfig`), is a thin adapter on
top that a caller builds from atom coordinates; everything downstream of
:class:`~precis.viz3d.primitives.Scene3` is domain-agnostic so ``se``
atomic designs, bare ``structure`` cells, and later ``cad``/envelope views
all share one occlusion/shading/scalebar implementation.

**Unit contract**: unit-agnostic by construction. A :class:`.primitives.Scene3`
carries plain floats plus a ``unit_label`` string ("Å", "nm", "m", ...); the
render core never converts or assumes a unit — it only *displays* the label,
on the scalebar, which is the sole place a unit surfaces at all.
Callers own the unit (the ``stickfig`` adapter fixes it to Å because
:mod:`precis.structure` is Å-native — see that package's docstring).

**Refine contract**: ``refine`` (0/1/2, with ``r3`` raytrace reserved for a
later slice) governs render QUALITY only — wireframe vs. shaded, sorted vs.
unsorted, gradient vs. flat. It never changes scene geometry or which atoms/
bonds/blocks are present; that fidelity lives one layer up, on the source
kind's own relax/detail rung (e.g. ``structure``'s clean/ml/dft ladder). A
figure recipe's ``refine`` and a structure's relax rung are two independent
dials — this package only ever reads the former.
"""

from __future__ import annotations
