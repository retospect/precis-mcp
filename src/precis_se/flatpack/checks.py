"""Store-aware flat-pack checks — the ``view='drc'`` rows a pure tree pass
cannot produce, because they read the design's ``meta['flatpack']`` params
home rather than the block tree.

:func:`findings` rebuilds the :class:`~precis_se.flatpack.generator.Flatpack`
from the stored params — the same regeneration ``view='cut'`` does — and
checks the *geometry the cut file will carry*, not the cad scene the op
minted (the two derive from the same panel model; the store-free
ownership invariant is pinned by ``tests/test_flatpack_panels.py``).

``precis_se.drc.drc(tree)`` stays store-free by contract; the handler
appends these rows after the other store-aware passes in
``_render_drc``.

Checks (docs/backlog/flatpack-furniture-generator.md, build 3):

* ``flatpack_mating`` (error) — at every mated pair (a shared slab
  volume, :mod:`precis_se.flatpack.mating`), each panel's material inside
  the joint region — its fingers and tabs — must land inside the mate's
  gap or slot polygons with the resolved ``fit`` clearance on each side
  (``fit/2``, within 1e-6 mm), and every gap component inside the region
  must contain a tab or finger: every slot has a tab.
* ``flatpack_min_width`` (error) — no part narrower than ``2t`` anywhere:
  ``polygon.buffer(-t)`` must leave a non-empty polygon.
* ``flatpack_nest_overlap`` / ``flatpack_off_sheet`` (error) — after
  kerf, no two nested parts overlap, and every part lies inside the
  sheet margin.
* ``flatpack_shelf_sag`` (warn) — when ``shelf_load_n`` is set, a simply
  supported beam with the material's ``E``; warns past 3 mm. With the
  load unset, or a material with no ``E``, the finding says it skipped
  and why rather than inventing a figure.
* ``flatpack_unrecorded_figure`` (warn) — a material with no recorded
  ``fit``/``kerf`` for the machine names the value the generator used
  (its ``FigureEcho``).
* ``flatpack_utilisation`` (info) — sheet utilisation, always.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from precis_se.flatpack.generator import Flatpack, flatpack_box, placed_polygon
from precis_se.flatpack.mating import _box_in_uv, mated_pairs, material_in_mate
from precis_se.flatpack.panels import FlatpackError, _fmt
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store
    from precis_se.ops import SeTree

_TOL_AREA = 1e-6
_TOL_LEN = 1e-6
#: Warn past this much shelf sag (mm).
SAG_LIMIT_MM = 3.0


def _regen(params: dict[str, Any]) -> Flatpack:
    kw = dict(params)
    if kw.get("sheet") is not None:
        kw["sheet"] = tuple(kw["sheet"])
    return flatpack_box(**kw)


def findings(store: Store, tree: SeTree, ref_id: int) -> list[ValidationIssue]:
    """The flat-pack rows for ``view='drc'`` — empty for a design with no
    ``meta['flatpack']`` (the op never ran on it)."""
    ref = store.get_ref(kind="se", id=ref_id)
    params = (ref.meta or {}).get("flatpack") if ref is not None else None
    if not isinstance(params, dict):
        return []
    try:
        fp = _regen(params)
    except (FlatpackError, TypeError, KeyError) as exc:
        return [
            ValidationIssue(
                rule="flatpack_regen",
                subject="meta['flatpack']",
                detail=(
                    f"the stored flatpack params no longer rebuild the panels: {exc}"
                ),
                severity="error",
            )
        ]
    out: list[ValidationIssue] = []
    out += _mating_findings(fp)
    out += _width_findings(fp)
    out += _nesting_findings(fp)
    out += _sag_finding(fp)
    out += _figure_findings(fp)
    w, h = fp.nesting.sheet
    out.append(
        ValidationIssue(
            rule="flatpack_utilisation",
            subject="sheet",
            detail=(
                f"sheet {_fmt(w)} x {_fmt(h)} mm, {len(fp.panels)} parts, "
                f"utilisation {100 * fp.utilisation:.1f} %"
            ),
            severity="info",
        )
    )
    return out


def _mating_findings(fp: Flatpack) -> list[ValidationIssue]:
    """error — every finger/tab into its mate's gap/slot with the ``fit``
    clearance; every gap component holding someone's tab. Checked both
    ways per pair. A three-way corner gap (the corner cube belongs to the
    side) is filled by the third panel, so occupancy counts *any* other
    panel's material in the joint, not only the partner's."""
    out: list[ValidationIssue] = []
    clearance = fp.figures.fit / 2
    for m in mated_pairs(fp.panels):
        for src, dst, region_src, region_dst in (
            (m.a, m.b, m.region_a, m.region_b),
            (m.b, m.a, m.region_b, m.region_a),
        ):
            subject = f"{m.a.id}—{m.b.id}"
            fingers = material_in_mate(src, dst, region_src)
            dst_material = dst.polygon.intersection(region_dst)
            hit = fingers.intersection(dst_material)
            if hit.area > _TOL_AREA:
                out.append(
                    ValidationIssue(
                        rule="flatpack_mating",
                        subject=subject,
                        detail=(
                            f"{src.id}'s finger/tab overlaps {dst.id}'s "
                            f"material by {_fmt(hit.area)} mm² in their "
                            "shared seam — outside every slot and gap"
                        ),
                        severity="error",
                    )
                )
            elif not fingers.is_empty:
                gap = fingers.distance(dst_material)
                if gap < clearance - _TOL_LEN:
                    out.append(
                        ValidationIssue(
                            rule="flatpack_mating",
                            subject=subject,
                            detail=(
                                f"{src.id}'s finger/tab sits "
                                f"{_fmt(gap)} mm from {dst.id}'s "
                                f"material — the fit clearance is "
                                f"{_fmt(clearance)} mm a side"
                            ),
                            severity="error",
                        )
                    )
            slots = region_dst.difference(dst.polygon)
            if slots.is_empty:
                continue
            # Whoever fills each gap: the partner's tab, or a third
            # panel's corner material where three panels meet.
            fill = unary_union(
                [
                    material_in_mate(
                        p,
                        dst,
                        _box_in_uv(m.lo, m.hi, p.frame) or Polygon(),
                    )
                    for p in fp.panels
                    if p.id != dst.id
                ]
            )
            comps = list(slots.geoms) if hasattr(slots, "geoms") else [slots]
            for comp in comps:
                if comp.area <= _TOL_AREA:
                    continue
                if comp.intersection(fill).area <= _TOL_AREA:
                    cb = comp.bounds
                    out.append(
                        ValidationIssue(
                            rule="flatpack_mating",
                            subject=subject,
                            detail=(
                                f"{dst.id} has a gap/slot in the seam "
                                f"with no tab in it "
                                f"(at u,v ≈ {_fmt((cb[0] + cb[2]) / 2)},"
                                f" {_fmt((cb[1] + cb[3]) / 2)} mm in "
                                f"{dst.id}'s frame)"
                            ),
                            severity="error",
                        )
                    )
    return out


def _width_findings(fp: Flatpack) -> list[ValidationIssue]:
    """error — a part with no spot ``2t`` wide: ``buffer(-t)`` empty."""
    out: list[ValidationIssue] = []
    for p in fp.panels:
        if p.polygon.buffer(-p.thickness).is_empty:
            out.append(
                ValidationIssue(
                    rule="flatpack_min_width",
                    subject=p.id,
                    detail=(
                        f"panel {p.id!r} ({p.name}) is narrower than "
                        f"2t = {_fmt(2 * p.thickness)} mm everywhere — "
                        "buffer(-t) leaves nothing"
                    ),
                    severity="error",
                )
            )
    return out


def _nesting_findings(fp: Flatpack) -> list[ValidationIssue]:
    """error — post-kerf parts overlapping, or lying outside the sheet
    margin."""
    out: list[ValidationIssue] = []
    placed = {
        pl.part: placed_polygon(fp.cut_polygons[pl.part], pl.x, pl.y, pl.rotated)
        for pl in fp.nesting.placements
    }
    w, h = fp.nesting.sheet
    m = fp.nesting.margin
    inside = box(m, m, max(m, w - m), max(m, h - m))
    for pid, poly in placed.items():
        over = poly.difference(inside)
        if over.area > _TOL_AREA:
            out.append(
                ValidationIssue(
                    rule="flatpack_off_sheet",
                    subject=pid,
                    detail=(
                        f"part {pid!r} lies {_fmt(over.area)} mm² outside "
                        f"the {_fmt(m)} mm sheet margin"
                    ),
                    severity="error",
                )
            )
    ids = sorted(placed)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            hit = placed[a].intersection(placed[b])
            if hit.area > _TOL_AREA:
                out.append(
                    ValidationIssue(
                        rule="flatpack_nest_overlap",
                        subject=f"{a}—{b}",
                        detail=(
                            f"nested parts {a} and {b} overlap by "
                            f"{_fmt(hit.area)} mm² after kerf"
                        ),
                        severity="error",
                    )
                )
    return out


def _sag_finding(fp: Flatpack) -> list[ValidationIssue]:
    """warn — a simply supported shelf under ``shelf_load_n`` past 3 mm;
    honest skips when the load or ``E`` is missing."""
    if not fp.params["shelves"]:
        return []
    load = fp.params["shelf_load_n"]
    e_gpa = fp.material.E_gpa
    if load is None:
        return [
            ValidationIssue(
                rule="flatpack_shelf_sag",
                subject="shelves",
                detail="shelf sag not checked — shelf_load_n unset",
                severity="warn",
            )
        ]
    if e_gpa is None:
        return [
            ValidationIssue(
                rule="flatpack_shelf_sag",
                subject="shelves",
                detail=(
                    f"shelf sag not checked — material {fp.material.slug} "
                    "has no recorded E"
                ),
                severity="warn",
            )
        ]
    t = fp.t
    span = fp.params["W"] - 2 * t
    width = fp.params["D"] - t
    inertia = width * t**3 / 12
    e = e_gpa * 1000.0  # GPa → N/mm²
    delta = 5 * float(load) * span**3 / (384 * e * inertia)
    detail = (
        f"shelf sags {_fmt(delta)} mm under {_fmt(float(load))} N "
        f"(span {_fmt(span)} mm, limit {_fmt(SAG_LIMIT_MM)} mm)"
    )
    return [
        ValidationIssue(
            rule="flatpack_shelf_sag",
            subject="shelves",
            detail=detail,
            severity="warn" if delta > SAG_LIMIT_MM else "info",
        )
    ]


def _figure_findings(fp: Flatpack) -> list[ValidationIssue]:
    """warn — a figure with no material record names the value used."""
    out: list[ValidationIssue] = []
    for name, value, source in (
        ("fit", fp.figures.fit, fp.figures.fit_source),
        ("kerf", fp.figures.kerf, fp.figures.kerf_source),
    ):
        if source.startswith("default:"):
            out.append(
                ValidationIssue(
                    rule="flatpack_unrecorded_figure",
                    subject=name,
                    detail=(
                        f"{name} {_fmt(value)} mm used — "
                        f"{source.removeprefix('default: ')}"
                    ),
                    severity="warn",
                )
            )
    return out
