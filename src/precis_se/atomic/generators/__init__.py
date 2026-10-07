"""Parametric block factories — the IC-design PCell, imported
(docs/backlog/nm-kind.md "Generators — parametric block factories"). A
generator is a pure function ``params → block`` (:class:`GeneratedBlock`):
for chemistry families where the math fixes the atoms (a ``(n, m)``
nanotube's radius, a fullerene's Goldberg-construction vertex count), the
LLM never guesses a single coordinate — the closed-form geometry runs
instead, deterministic and reproducible. This is the *first* of the three
fill paths nm-kind.md's "Generators" section orders (generator →
``nm_propose`` LLM job → hand ops): generators shrink what the LLM must
invent.

Every generator here is **pure** — no store access, the ``ops.py``
discipline — taking a JSON-shaped ``params`` dict and returning a
:class:`GeneratedBlock` (:mod:`precis_se.atomic.generators._types`) carrying
everything the handler-level ``generate`` op
(:meth:`precis_nm.handler.NmHandler._generate`, the same
``import_fragment``/``bind_structure`` store-aware-interception pattern
``ops.py``'s module docstring already documents) needs to (1) add a new
block with the generated envelope, (2) add its ports, (3) mint a
``structure`` design holding the realized atoms/bonds, and (4) bind it —
never touching the store itself.

**Param validation is theorems failing loudly** (nm-kind.md): a rejected
parameter set raises :class:`~precis_se.atomic.generators._types.GeneratorError`
naming the violated constraint and its valid range/formula, before any
geometry runs — never a silent clamp or a NaN downstream (see
:mod:`precis_se.atomic.generators.sp2`'s module docstring for both families'
derivations). Every generator's build function also carries a provenance
note (the formula/construction used) into the returned block's
``provenance`` field, which the handler stores as the minted block's
``desc``.

:data:`GENERATORS` is the name → builder registry the ``generate`` op
looks up. Round 1 (slice 4a, build order (i)): ``cnt`` (single-wall carbon
nanotube, chiral rolling) and ``fullerene`` (C60, truncated icosahedron) —
both in :mod:`precis_se.atomic.generators.sp2`. Round 2 (build order (ii)):
``cone`` (nanohorn, wrapped-sheet disclination — also
:mod:`precis_se.atomic.generators.sp2`); nanobud fusion scope-checked and skipped
(design note at the end of that module). Round 3 (build order (ii)
continued): ``cyclodextrin`` (alpha/beta/gamma-CD,
:mod:`precis_se.atomic.generators.sugars`) — a two-path generator (rdkit conformer
+ a loud cavity-diameter check, falling back to a Cn-symmetric idealized
template) rather than the closed-form-only construction the sp² family
uses; L4 mechanics-ceiling metrics (build order (iii)) live in
:mod:`precis_se.atomic.mechanics`, not this registry. Round 4:
``hexfold`` (:mod:`precis_se.atomic.generators.hexfold_spec`) — the whole
curved-sp² family behind one ``spec`` text param (topology-only `.hx`
notation; sheets/tubes/cones/fullerenes/defects/attachments as one
compiler), with hexfold itself a lazy optional import, not a declared
dependency. Round 5: ``tpms``/``schwarzite``
(:mod:`precis_se.atomic.generators.tpms`) — periodic Schwarz P/D/gyroid
carbon scaffolds via :mod:`precis_surface`'s level-set/marching-cubes/dual
pipeline (docs/backlog/precis-surface-kernel.md "Slice 1 — the dual
route"), a preview scaffold ahead of the degree-controlled remesh loop.
Round 6: ``smooth_drum`` (:mod:`precis_se.atomic.generators.smooth_drum`) —
a sheet-stalk-drum carbon wrapper on a smooth surface of revolution with
exactly 12 pentagons and 12 heptagons, spring-relaxed and gated on
pyramidalisation.
Round 7: ``hexfold_scene`` (:mod:`precis_se.atomic.generators.hexfold_scene`)
-- several authored fillet feet on one sheet, planned by
:func:`precis_se.atomic.generators.authored_foot.plan_scene` and minted
tethered, with the planner's misses and top-joint caveats as findings.
Explicit sphere fillets now clear the existing conservative R_min estimate
as an authored-input policy before construction. This is deliberately not a
necessary physical bound: omitted room-capped defaults and lids stay unchanged.
The dedicated theta-p diagnostic reads recorded tethered scene measurements
in block views, so old provenance is inspectable without regenerating or
confusing trial/grid placeholders with scene evidence; saved reports survive.
The exclusive ``k3-sp2-120-z`` scene feature exposes a straight equal-120 Y
of three open sheets, reusing hexfold's private segment joiner and one
tethered stick pass. Public scene params are its replay input; no hx grammar
is invented. Its analytic seam and immediate neighbours are explicitly
pinned because plane-only tethers permit axial slip of the authored motif;
the remaining sheets relax under the existing forces. No spring threshold
changes or general rail topology are added. Its three fixed planes cannot
become a revolution-target receipt, so stored surface_deviation stays unavailable
while seam-ring, bond, angle and tether residual findings remain inspectable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis_se.atomic.generators._types import (
    GeneratedBlock,
    GeneratedPort,
    GeneratorError,
)
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.generators.hexfold_spec import build_hexfold
from precis_se.atomic.generators.smooth_drum import build_smooth_drum
from precis_se.atomic.generators.sp2 import build_cnt, build_cone, build_fullerene
from precis_se.atomic.generators.sugars import build_cyclodextrin
from precis_se.atomic.generators.tpms import build_tpms

Generator = Callable[[dict[str, Any]], GeneratedBlock]

#: name → builder, looked up by the ``generate`` op
#: (:meth:`precis_nm.handler.NmHandler._generate`). An unknown name is a
#: loud ``BadInput`` listing every registered name — the handler does that
#: lookup and the error mapping, not this module.
GENERATORS: dict[str, Generator] = {
    "cnt": build_cnt,
    "fullerene": build_fullerene,
    "cone": build_cone,
    "cyclodextrin": build_cyclodextrin,
    "hexfold": build_hexfold,
    "hexfold_scene": build_hexfold_scene,
    "tpms": build_tpms,
    "smooth_drum": build_smooth_drum,
    #: alias -- schwarzite-class carbon nets are the tpms family's whole
    #: point (module docstring), so the more chemistry-recognizable name
    #: reaches the same builder.
    "schwarzite": build_tpms,
}

__all__ = [
    "GENERATORS",
    "GeneratedBlock",
    "GeneratedPort",
    "Generator",
    "GeneratorError",
]
