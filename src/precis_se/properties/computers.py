"""Wall-side region computers — slice 2a of
docs/backlog/pocket-from-molecule.md; the first entries of
:data:`precis_se.properties.COMPUTERS` (region property layer slice B).

A computer answers "what does the bound wall actually present opposite
this region?" for the four measurands the slice-1 painter can score:

- ``net partial charge`` (e): the sum of the GFN2 Mulliken charges over
  the region's atoms.
- ``H-bond donor count`` / ``H-bond acceptor count``: the count of
  :class:`~precis_se.properties.surface.AtomClass` ``hbond`` over the
  region's atoms.
- ``electrostatic potential`` (V): the mean ``esp_V`` over the region's
  own surface points.

The painter (:func:`~precis_se.properties.surface.paint_surface`, GFN2
charges, :data:`~precis_se.properties.surface.SURFACE_SETTINGS`) runs
on the wall atoms within ``clearance + 3 Å`` of the region — the cluster a
dangling-bond wall fragment gives meaningful charges for. The region's
atoms come from :func:`precis_se.datums.resolve` (``atoms:``/``sites:``/
``mpatch:`` on the bound structure); the clearance is read off the pocket
shape that claims the region (``mol:<block>@<clearance>``, Å in the token
text), 0 when no pocket claims it — the official default (spec Q4).

Every entry has the ``(tree, measure) -> float | None`` signature and
carries a ``reason`` attribute refreshed on each call: why a ``None`` was
returned (no resolver wired, structure gone, the region resolves to an
error, no atoms in the cut, no GFN2 backend, the region's atoms expose no
surface points) — :func:`precis_se.drc.drc` reads it for
``measure_uncomputable``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from precis_se import datums as se_datums
from precis_se.measures import MeasureError, MeasureSpec

#: The shell around a region the painter covers (Å), on top of the
#: pocket's declared clearance: wall atoms whose centre sits within
#: ``clearance + _REGION_CUT_A`` of the region's atoms/points make the
#: painted cluster.
_REGION_CUT_A = 3.0

#: Datum kinds whose resolve yields wall atoms the painter can score.
_ATOM_REGION_KINDS = ("atoms", "sites", "trace_frame", "mpatch", "mol", "mcone")


class _Computer:
    """Adapt a ``(tree, measure) -> (value, reason)`` worker to the
    ``(tree, measure) -> float | None`` computer signature, keeping the
    last reason on the instance for ``measure_uncomputable``."""

    def __init__(self, fn: Callable[[Any, Any], tuple[float | None, str | None]]):
        self._fn = fn
        self.reason: str | None = None
        self.__name__ = getattr(fn, "__name__", "computer")

    def __call__(self, tree: Any, measure: Any) -> float | None:
        value, self.reason = self._fn(tree, measure)
        return value


def pocket_clearance_A(tree: Any, block: str, datum: str | None) -> float:
    """The clearance (Å) the pocket claiming ``datum`` as a region carries
    — parsed from its ``mol:<block>@<clearance>`` shape (the token writes
    Å; the selector stores metres). ``0`` when no pocket on ``block``
    claims the region or the shape parses to something else."""
    node = tree.blocks.get(block)
    if node is None or datum is None:
        return 0.0
    for pocket in node.pockets.values():
        if not any(se_datums.same_region(s, datum) for s in pocket.regions):
            continue
        shape = (pocket.shape or "").strip()
        if not shape.startswith("mol:"):
            continue
        try:
            sel = se_datums.parse_selector(shape)
        except MeasureError:
            continue
        clearance = getattr(sel, "clearance", None)
        if clearance is not None:
            return float(clearance) * 1e10
    return 0.0


def _painted_wall(
    tree: Any, measure: MeasureSpec
) -> tuple[Any, list[int]] | tuple[None, str]:
    """``(painted surface, indices into it of the region's atoms)`` for a
    wall-region measure, or ``(None, reason)``.

    For ``atoms:``/``sites:`` the painter's cluster is the owner block's
    atoms within ``clearance + 3 Å`` of the *selected* atoms; the region's
    atoms are the selected ones (an ``mcone:`` region likewise: its wall
    atoms inside the cone, the cluster around them). For ``mpatch:`` and
    ``mol:`` the resolve already cut the wall atoms facing the molecule —
    cluster and region are the same set.
    """
    datum = measure.datum or ""
    try:
        sel = se_datums.parse_selector(datum)
    except MeasureError:
        return None, f"datum {datum!r} does not parse"
    if sel.kind not in _ATOM_REGION_KINDS:
        return None, (
            f"datum {datum!r} is not an atom region (atoms:/sites:/mpatch:/"
            "mol:/mcone:) — nothing to paint"
        )
    block = tree.blocks.get(measure.block)
    if block is None:
        return None, f"measure names missing block {measure.block!r}"
    try:
        rdat = se_datums.resolve(tree, block, sel, pin=measure.datum_pin)
    except MeasureError as exc:
        return None, f"datum {datum!r}: {exc}"
    if rdat.error is not None:
        return None, rdat.error
    region_ordinals = rdat.atoms
    if not region_ordinals:
        return None, f"region {datum!r} resolved to no atoms"
    owner_name = sel.instance if sel.kind in ("atoms", "sites") else measure.block
    got = se_datums.posed_structure_atoms(tree, owner_name or "")
    if got is None:
        return None, f"the bound structure of {owner_name!r} cannot be loaded here"
    labels, elements, coords_A, formal, _meta = got
    cut_A = pocket_clearance_A(tree, measure.block, datum) + _REGION_CUT_A
    if sel.kind in ("mpatch", "mol"):
        cluster = list(region_ordinals)
    else:
        anchor = coords_A[list(region_ordinals)]
        dist = np.linalg.norm(coords_A[:, None, :] - anchor[None, :, :], axis=2)
        cluster = sorted(int(i) for i in np.nonzero(dist.min(axis=1) <= cut_A)[0])
    if not cluster:
        return None, f"no wall atoms within {cut_A:g} Å of the region"
    from precis_se.properties import surface as se_surface

    if len(cluster) > se_surface.SURFACE_MAX_ATOMS:
        return None, (
            f"{len(cluster)} atoms in the region's cluster is over the "
            f"{se_surface.SURFACE_MAX_ATOMS}-atom paint limit"
        )
    cl_labels = [labels[i] for i in cluster]
    cl_elements = [elements[i] for i in cluster]
    cl_coords = coords_A[cluster]
    cl_formal = [formal[i] for i in cluster]
    electrons = se_surface.check_charge_multiplicity(cl_elements, sum(cl_formal), None)
    multiplicity = 1 if electrons % 2 == 0 else 2
    try:
        sp = se_surface.gfn2_single_point(
            cl_elements,
            cl_coords,
            charge=sum(cl_formal),
            multiplicity=multiplicity,
        )
    except Exception as exc:
        return None, f"GFN2 single point failed: {exc}"
    painted = se_surface.paint_surface(
        cl_labels, cl_elements, cl_coords, sp.charges, cl_formal
    )
    index = {o: k for k, o in enumerate(cluster)}
    region_idx = [index[o] for o in region_ordinals if o in index]
    return painted, region_idx


def _empty_cone(tree: Any, measure: MeasureSpec) -> bool:
    """Does the measure's ``mcone:`` region resolve cleanly to no wall
    atoms? Then a count over it is a real 0, not an uncomputable value."""
    try:
        sel = se_datums.parse_selector(measure.datum or "")
    except MeasureError:
        return False
    block = tree.blocks.get(measure.block)
    if sel.kind != "mcone" or block is None:
        return False
    rdat = se_datums.resolve(tree, block, sel)
    return rdat.error is None and not rdat.atoms


def cone_atom_count(
    tree: Any, measure: MeasureSpec
) -> tuple[float | None, str | None] | None:
    """The realised value of a ``count`` measure on an ``mcone:`` region —
    the number of wall atoms inside the cone (slice 3's keep-open ``max
    0`` and hold ``min 1`` requirements) — as ``(value, reason)``.
    ``None`` when the measure is not a count on a cone, so callers fall
    through to their usual path. DRC (``pocket_keep_open``,
    ``measure_unmet``) and ``view='pockets'`` read it."""
    if measure.unit != "count" or measure.measurand not in (None, "count"):
        return None
    try:
        sel = se_datums.parse_selector(measure.datum or "")
    except MeasureError:
        return None
    if sel.kind != "mcone":
        return None
    block = tree.blocks.get(measure.block)
    if block is None:
        return None, f"measure names missing block {measure.block!r}"
    rdat = se_datums.resolve(tree, block, sel)
    if rdat.error is not None:
        return None, rdat.error
    return float(len(rdat.atoms)), None


def _net_partial_charge(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """Σ GFN2 Mulliken charges (e) over the region's atoms."""
    painted, region_idx = _painted_wall(tree, measure)
    if painted is None:
        return None, str(region_idx)
    return float(np.asarray(painted.atom_charges)[region_idx].sum()), None


def _hbond_count(tree: Any, measure: Any, want: str) -> tuple[float | None, str | None]:
    if _empty_cone(tree, measure):
        return 0.0, None
    painted, region_idx = _painted_wall(tree, measure)
    if painted is None:
        return None, str(region_idx)
    return (
        float(sum(1 for i in region_idx if painted.classes[i].hbond == want)),
        None,
    )


def _hbond_donor_count(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """Count of ``AtomClass.hbond == 'donor'`` over the region's atoms."""
    return _hbond_count(tree, measure, "donor")


def _hbond_acceptor_count(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """Count of ``AtomClass.hbond == 'acceptor'`` over the region's atoms."""
    return _hbond_count(tree, measure, "acceptor")


def _electrostatic_potential(
    tree: Any, measure: Any
) -> tuple[float | None, str | None]:
    """Mean ``esp_V`` (volts) over the region's own surface points."""
    painted, region_idx = _painted_wall(tree, measure)
    if painted is None:
        return None, str(region_idx)
    mine = np.isin(painted.owner, np.asarray(region_idx, dtype=np.int64))
    if not mine.any():
        return None, "the region's atoms expose no surface points"
    return float(np.asarray(painted.esp_V)[mine].mean()), None


#: measurand slug → computer, registered in
#: :data:`precis_se.properties.COMPUTERS` — the four measurands the
#: painter scores (the categorical hydrophobicity index has no computer:
#: it stays declared intent).
WALL_COMPUTERS: dict[str, Any] = {
    "net-partial-charge": _Computer(_net_partial_charge),
    "h-bond-donor-count": _Computer(_hbond_donor_count),
    "h-bond-acceptor-count": _Computer(_hbond_acceptor_count),
    "electrostatic-potential": _Computer(_electrostatic_potential),
}
