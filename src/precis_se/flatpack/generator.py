"""``flatpack_box``: parameters → panels → nesting → sheet job.

The one entry point agents and the (later) se op call. All lengths are mm.
``fit`` and ``kerf`` resolve explicit > the material's recorded figure for
the machine (``laser`` at ``cutter_d = 0``, ``cnc`` otherwise) > 0 mm, and
:class:`FigureEcho` says which figure was used and where it came from —
the job's notes carry the same sentence into every exported file.
``shelf_load_n`` is accepted and echoed for the shelf-sag check of the se
build; this module does not compute with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from shapely import affinity
from shapely.geometry import Polygon

from precis.sheet.model import Layer, Part, Polyline, Sheet, SheetJob
from precis_se.flatpack.materials import DEFAULT_MATERIAL, Material, material
from precis_se.flatpack.nest import Nesting, nest, spacing_for
from precis_se.flatpack.panels import (
    FlatpackError,
    Panel,
    _fmt,
    box_panels,
    kerf_offset,
    rings,
)

CUT_LAYER = "cut"


@dataclass(frozen=True)
class FigureEcho:
    machine: str
    fit: float
    kerf: float
    fit_source: str
    kerf_source: str

    def sentences(self) -> list[str]:
        return [
            f"fit {_fmt(self.fit)} mm ({self.fit_source})",
            f"kerf {_fmt(self.kerf)} mm ({self.kerf_source})",
        ]


def resolve_figures(
    mat: Material, *, fit: float | None, kerf: float | None, cutter_d: float
) -> FigureEcho:
    machine = "laser" if cutter_d == 0 else "cnc"
    rec = mat.recorded(machine)

    def pick(
        name: str, explicit: float | None, recorded: float | None
    ) -> tuple[float, str]:
        if explicit is not None:
            return float(explicit), f"explicit {name}"
        if recorded is not None:
            return float(recorded), f"{mat.slug} {machine} record"
        return 0.0, f"default: no recorded {name} for {mat.slug} on {machine}"

    f, fs = pick("fit", fit, rec.fit)
    k, ks = pick("kerf", kerf, rec.kerf)
    return FigureEcho(machine, f, k, fs, ks)


@dataclass(frozen=True)
class Flatpack:
    """Everything one ``flatpack_box`` call produced."""

    params: dict[str, Any]
    material: Material
    t: float
    figures: FigureEcho
    panels: tuple[Panel, ...]
    #: Post-kerf cut polygons in each panel's own frame, by panel id.
    cut_polygons: dict[str, Polygon]
    nesting: Nesting
    job: SheetJob

    def panel(self, pid: str) -> Panel:
        for p in self.panels:
            if p.id == pid:
                return p
        raise KeyError(pid)

    @property
    def utilisation(self) -> float:
        w, h = self.nesting.sheet
        return sum(p.polygon.area for p in self.panels) / (w * h)


def flatpack_box(
    W: float,
    H: float,
    D: float,
    *,
    material: str = DEFAULT_MATERIAL,
    t: float | None = None,
    shelves: Sequence[float] = (),
    joint: str = "finger",
    fit: float | None = None,
    kerf: float | None = None,
    cutter_d: float = 0.0,
    sheet: tuple[float, float] | None = None,
    shelf_load_n: float | None = None,
) -> Flatpack:
    """A ``W × H × D`` open-front box with shelves, nested on one sheet.
    Raises :class:`FlatpackError` (a :class:`~precis_se.flatpack.nest.NestingError`
    when the given sheet is too small)."""
    mat = material_of(material)
    t_used = float(mat.t_actual if t is None else t)
    if t_used <= 0:
        raise FlatpackError(f"t must be positive, not {_fmt(t_used)}")
    figures = resolve_figures(mat, fit=fit, kerf=kerf, cutter_d=cutter_d)
    panels = box_panels(
        W,
        H,
        D,
        t=t_used,
        shelves=shelves,
        joint=joint,
        fit=figures.fit,
        cutter_d=cutter_d,
    )
    cut = {p.id: kerf_offset(p.polygon, figures.kerf) for p in panels}
    spacing = spacing_for(figures.kerf, cutter_d)
    nesting = nest(
        [(p.id, *_size(cut[p.id])) for p in panels], spacing=spacing, sheet=sheet
    )
    params = {
        "W": W,
        "H": H,
        "D": D,
        "material": mat.slug,
        "t": t_used,
        "shelves": [float(h) for h in shelves],
        "joint": joint,
        "fit": fit,
        "kerf": kerf,
        "cutter_d": cutter_d,
        "sheet": list(sheet) if sheet else None,
        "shelf_load_n": shelf_load_n,
    }
    job = emit_sheet_job(panels, cut, nesting, material=mat, t=t_used, figures=figures)
    return Flatpack(params, mat, t_used, figures, tuple(panels), cut, nesting, job)


def material_of(slug: str) -> Material:
    try:
        return material(slug)
    except KeyError as exc:
        raise FlatpackError(str(exc)) from None


def _size(poly: Polygon) -> tuple[float, float]:
    minx, miny, maxx, maxy = poly.bounds
    return (maxx - minx, maxy - miny)


def placed_polygon(poly: Polygon, x: float, y: float, rotated: bool) -> Polygon:
    """``poly`` rotated 90° if asked, then moved so its bounding box starts at
    ``(x, y)`` on the sheet."""
    if rotated:
        poly = affinity.rotate(poly, 90, origin=(0, 0))
    minx, miny, _, _ = poly.bounds
    return affinity.translate(poly, x - minx, y - miny)


def emit_sheet_job(
    panels: Sequence[Panel],
    cut: dict[str, Polygon],
    nesting: Nesting,
    *,
    material: Material,
    t: float,
    figures: FigureEcho,
) -> SheetJob:
    """One ``cut`` layer with every panel's slots then outline, in nesting
    order. No laser settings on the layer: the exporter's defaults apply and
    its notes say so, until the material carries recorded settings."""
    by_id = {p.id: p for p in panels}
    w, h = nesting.sheet
    job = SheetJob(
        Sheet(w, h, material.slug, t),
        layers=[Layer(CUT_LAYER, "cut")],
        notes=[
            *figures.sentences(),
            f"nesting: margin and spacing {_fmt(nesting.margin)} mm; sheet "
            + ("given" if nesting.sheet_given else "derived from the strip packing")
            + f"; utilisation {100 * sum(p.polygon.area for p in panels) / (w * h):.1f} %",
        ],
    )
    for pl in nesting.placements:
        panel = by_id[pl.part]
        job.parts.append(
            Part(
                panel.id,
                panel.name,
                pose={**panel.frame.as_dict(), "rotated_on_sheet": pl.rotated},
                thickness_mm=t,
            )
        )
        outer, *inners = rings(placed_polygon(cut[pl.part], pl.x, pl.y, pl.rotated))
        for ring in inners:
            job.shapes.append(Polyline(CUT_LAYER, ring, part=panel.id, inner=True))
        job.shapes.append(Polyline(CUT_LAYER, outer, part=panel.id))
    return job.validate()
