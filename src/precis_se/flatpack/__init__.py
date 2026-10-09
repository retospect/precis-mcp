"""precis_se.flatpack — the flat-pack furniture generator: a box with shelves
cut from one sheet, joinery derived from the sheet thickness, nested and
exported for the laser (SVG) and the Maslow CNC (DXF) through the shared
sheet job (:mod:`precis.sheet`).

The third way a maker builds a thing after solids (se) and printed fields:
flat parts cut from one sheet and slotted together. Design and Reto's
rulings: docs/backlog/flatpack-furniture-generator.md (laser first; 3 mm
corrugated cardboard first; finger joints default, plain butt edge as the
option; the first box is a 50 mm cube; a shared sheet job under the
generator from build 1).

**Source of truth is a 2-D panel model, not a 3-D solid**
(:mod:`precis_se.flatpack.panels`): deriving outlines from a solid would
need a section-to-polygon extractor cad does not have, while a rectilinear
outline with its fingers, slots and dog-bones plus a 3-D frame is both the
cut geometry and enough to place the panel in a design. The geometry rules
live in that module's docstring; the invariant the tests pin is ownership:
at ``fit = 0`` the panels tile the box shell exactly, so every finger meets
a gap and every corner cube has one owner.

Shape, in call order:

* :mod:`precis_se.flatpack.materials` — a constants module: ``t_nominal``,
  ``t_actual``, ``E`` where known, and the per-machine ``fit``/``kerf``
  that seated on a real cut (all ``None`` until Reto's first cube).
* :mod:`precis_se.flatpack.panels` — ``box_panels`` (finger/straight
  joints, shelves with tabs and slots, ``fit``, dog-bones) and the pure
  helpers ``kerf_offset`` (mitre offset) and ``rings`` (deterministic
  contour order for files).
* :mod:`precis_se.flatpack.nest` — skyline rectangle packer over post-kerf
  bounding boxes; refuses a too-small sheet naming the mm² shortfall, or
  derives the sheet when none is given.
* :mod:`precis_se.flatpack.generator` — ``flatpack_box(...)`` ties them
  together and emits the :class:`~precis.sheet.model.SheetJob` (one ``cut``
  layer, slots before outlines, parts in nesting order); the exporters in
  :mod:`precis.sheet` take it from there.

**Seams.** Nothing here touches the store or a handler; the se op, the
params home (``meta['flatpack']``), ``view='cut'`` and the store-aware
checks are the next build and will sit in ``precis_se.atomic.apply`` /
``precis_se.handler`` / ``flatpack/checks.py``. Living hinges are a
separate item. Units are mm only, here and in the sheet job.
"""

from __future__ import annotations

from precis_se.flatpack.generator import FigureEcho, Flatpack, flatpack_box
from precis_se.flatpack.materials import MATERIALS, Material
from precis_se.flatpack.nest import Nesting, NestingError, Placement
from precis_se.flatpack.panels import FlatpackError, Frame, Panel, box_panels

__all__ = [
    "MATERIALS",
    "FigureEcho",
    "Flatpack",
    "FlatpackError",
    "Frame",
    "Material",
    "Nesting",
    "NestingError",
    "Panel",
    "Placement",
    "box_panels",
    "flatpack_box",
]
