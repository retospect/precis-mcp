"""precis.sheet — the shared **sheet job**: one planar cut job, several machines.

A sheet job is the 2-D artefact under everything that cuts flat stock: the
flat-pack generator (:mod:`precis_se.flatpack`), later the Cricut and PCB's
mechanical layers (edge cuts, NPTH drills, the EWOD gasket). Reto's three 2-D
machines want the same object — a job of **layers**, each layer exactly one
operation, with settings per machine — so the exporters are written once
against this model instead of once per emitter
(docs/backlog/flatpack-furniture-generator.md, se-machine-design-7 option 1).

**Neutral by contract.** Pure data and pure functions: no DB, no handler, and
no import from ``precis.pcb`` or ``precis_se`` — both of those emit *into*
this package, never the other way (PCB never reads a sheet job; ruled on
se-machine-design-7).

**Model** (:mod:`precis.sheet.model`). A :class:`SheetJob` is a
:class:`Sheet` (size, material, thickness), ordered :class:`Layer` rows (name
+ one ``op`` of :data:`OPS` + a ``settings`` mapping keyed by machine), shapes
(:class:`Polyline`, :class:`Circle` — mm, each on one declared layer,
optionally tied to a :class:`Part` and flagged inner/outer) and parts (id,
name, optional 3-D pose and thickness). Coordinates are **mm, x right, y up,
origin at the sheet's bottom-left**; the SVG writer flips y. Layer order in
the job is the cut order a machine runs; :func:`order_layers` sorts into the
default ``engrave_raster → engrave_vector → score → drill → cut`` so parts
are cut free last. ``engrave_raster`` is reserved in this build: the model
accepts it and every exporter refuses it by layer name.

**Exporters**, one module each; each writes the ops its machine can do and
refuses the rest by layer name — never a silent drop:

- :mod:`precis.sheet.svg_laser` — LightBurn-facing SVG. mm units, hairline
  strokes, one ``<g id=layer data-op data-power-pct data-speed-mm-s
  data-passes>`` per layer coloured from LightBurn's palette in layer order
  (LightBurn maps colour → its own layer and reads no settings from the
  file, so the ``data-`` attributes and the colour → settings table in the
  leading comment are for the operator). Document order is cut order:
  layers in job order, parts in nesting order, a part's inner contours
  before its outline.
- :mod:`precis.sheet.dxf` — hand-written ASCII DXF R12 (``POLYLINE``/
  ``VERTEX``, ``CIRCLE``), one DXF layer per vector layer, for the Maslow
  CNC. R12 has no units header; mm is stated in a group-999 comment. No
  ``ezdxf``: the tree has no DXF writer and gains no dependency for one.

A layer carrying no settings for the exporting machine exports with that
machine's defaults (:data:`precis.sheet.svg_laser.LASER_DEFAULTS`) and the
notes say so. Notes travel in the returned :class:`Export` and, as a leading
comment, in the file itself, so the file alone tells the operator what it
assumes.
"""

from __future__ import annotations

from precis.sheet.model import (
    DEFAULT_OP_ORDER,
    MACHINES,
    OPS,
    Circle,
    Export,
    Layer,
    Part,
    Polyline,
    Shape,
    Sheet,
    SheetExportError,
    SheetJob,
    SheetJobError,
    order_layers,
)

__all__ = [
    "DEFAULT_OP_ORDER",
    "MACHINES",
    "OPS",
    "Circle",
    "Export",
    "Layer",
    "Part",
    "Polyline",
    "Shape",
    "Sheet",
    "SheetExportError",
    "SheetJob",
    "SheetJobError",
    "order_layers",
]
