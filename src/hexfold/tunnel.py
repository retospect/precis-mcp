"""Tunnel: a tube closed between two reservoir sheets -- sheet | tube | sheet.

The nanoreactor's first tunnel stage (docs/backlog/threads/nanoreactor.md,
slice H1): each tube end seats into a hole cut by :mod:`hexfold.opening`
in its own flat sheet, so the bore is walled off from the outside except
through the mouths -- a closed reservoir tunnel, not a fused wireframe.
The joint topology is whatever the opening solver mints: fused
rim/boundary pairs, defect-ring faces, leftover dangling atoms marked for
termination.

One rigid tube, two openings: the tube's rotation about its own axis is
shared, so the twist scan evaluates both ends at each angle and keeps the
joint minimum; each opening then cuts its own sheet at its own best
scale.

Sheets are ``_sheet`` blocks -- rectangular graphene patches, flat in
their own planes -- because a reservoir wall is not a grammar object.  A
plain ``tube(n,n)`` end gives ``rim_loop_port`` (armchair only, other
chiralities refused by name); a rail tube's end gives ``rim_loop_end`` --
its boundary zigzags inward along each rib, so the sheet hole follows the
projected star outline and leftover rib tips fall to termination.

The assembled :class:`Net` carries ``regions`` for the sheets, the tube
and each junction, and its ``report`` holds one ``opening.*`` finding per
end.  Analytic seed only; callers relax via stick with the sheets
tethered to their planes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

import numpy as np

from .build import Atom, Net, build
from .ids import AtomPath
from .join import Block, block_from_net
from .lattice import Lattice, Site
from .opening import (
    Opening,
    RimLoop,
    apply_opening,
    rim_loop_end,
    rim_loop_port,
    scan_opening,
    twist_coords,
)
from .railtube import rail_tube
from .report import Finding, Report, Severity
from .text import Spec
from .y_junction import _sheet

#: degrees scanned for the shared twist about the tube axis (the rim
#: polygon's own symmetry period is at most 60 deg)
_TWISTS = tuple(range(0, 60, 2))
#: a sheet must clear the rim polygon by this margin (Angstrom) on every
#: side so the hole never merges with the outer rim
_MARGIN = 3.0

#: junction relaxation decays within 10-12 A (4-5 rings); openings closer
#: than this margin are reported rather than refused (memory me482127)
_MIN_JUNCTION_SEP_A = 25.0


@dataclass(frozen=True)
class Junction:
    """One sheet/tube mouth: which composite ordinals joined, the defect
    ring's face census, and boundary atoms left dangling."""

    #: fused pairs, (sheet_ord, tube_ord) composite ordinals
    fused: tuple[tuple[int, int], ...]
    #: minted seam-face atom ordinals (composite), one tuple per face
    faces: tuple[tuple[int, ...], ...]
    #: unbonded boundary atoms, composite ordinals
    unbonded_sheet: tuple[int, ...]
    unbonded_tube: tuple[int, ...]
    #: every atom in the junction zone (pairs + face members + unbonded)
    atoms: tuple[int, ...]


@dataclass(frozen=True)
class Tunnel:
    """The assembled reservoir tunnel's bookkeeping for callers."""

    kind: str  # "armchair" | "rail"
    sheet_size: tuple[int, int]
    sheet_lo: tuple[int, ...]
    sheet_hi: tuple[int, ...]
    tube_atoms: tuple[int, ...]
    junctions: tuple[Junction, Junction]  # low end, high end
    coords: np.ndarray
    sheet_plane_z: tuple[float, float]
    twist_deg: float
    #: rail tube only: per-rib strip atoms and per-seam seam atoms,
    #: composite ordinals -- the tunnel's lumen catalysis bookkeeping
    strip_atoms: tuple[tuple[int, ...], ...] = ()
    seam_atoms: tuple[tuple[int, ...], ...] = ()


def _tube_block(
    kind: str, spec: tuple
) -> tuple[Block, Net, tuple[tuple[int, ...], ...], tuple[tuple[int, ...], ...]]:
    """Resolve the tube spec into ``(block, net, strip_atoms, seam_atoms)``;
    the last two are populated for rail tubes only."""
    if kind == "armchair":
        n, periods = spec
        net = build(f"hexfold 0.2\nt: tube({n},{n}, len={periods})\n")
        return block_from_net(net, np.asarray(net.seed3)), net, (), ()
    if kind == "rail":
        rows, periods, strip_rows, rails, pores = spec
        net, rt = rail_tube(periods, rows, strip_rows, rails, pores=pores)
        return (
            block_from_net(net, np.asarray(net.seed3)),
            net,
            rt.strip_atoms,
            rt.seam_atoms,
        )
    raise ValueError(f"tunnel tube kind must be 'armchair' or 'rail', got {kind!r}")


def _end_rims(block: Block, net: Net, kind: str) -> tuple[RimLoop, RimLoop]:
    """(low, high) rim loops along +z."""
    if kind == "armchair":
        lo = rim_loop_port(net, "in")
        hi = rim_loop_port(net, "out")
        zlo = np.asarray(block.coords)[list(lo.atoms), 2].mean()
        zhi = np.asarray(block.coords)[list(hi.atoms), 2].mean()
        return (lo, hi) if zlo <= zhi else (hi, lo)
    axis = np.array([0.0, 0.0, 1.0])
    return (
        rim_loop_end(block.coords, block.bonds, axis, "low"),
        rim_loop_end(block.coords, block.bonds, axis, "high"),
    )


def tunnel_reservoirs(
    tube: tuple,
    sheet: tuple[int, int],
    *,
    kind: str = "armchair",
) -> tuple[Net, Tunnel]:
    """Assemble sheet | tube | sheet with opening-solver joints.

    ``tube``: for ``kind="armchair"``, ``(n, periods)`` building
    ``tube(n,n, len=periods)``; for ``kind="rail"``, ``(rows, periods,
    strip_rows, rails, pores)`` passed to :func:`rail_tube`.
    ``sheet``: ``(x_periods, row_pairs)`` per reservoir sheet, each
    centred on its mouth.

    Refuses when a sheet cannot clear the rim polygon plus margin, or
    when the shared-twist scan seats neither end.
    """
    if kind not in ("armchair", "rail"):
        raise ValueError(f"tunnel kind must be 'armchair' or 'rail', got {kind!r}")
    sx, sy = sheet
    lattice = Lattice()
    sigma = lattice.sigma_A
    tblk, tnet, strips0, seams0 = _tube_block(kind, tube)
    rim_lo, rim_hi = _end_rims(tblk, tnet, kind)

    tc = np.asarray(tblk.coords, dtype=float)
    axis = np.array([0.0, 0.0, 1.0])
    cen_lo = tc[list(rim_lo.atoms)].mean(axis=0)
    cen_hi = tc[list(rim_hi.atoms)].mean(axis=0)
    # each rim's dangling plane seats one sigma off its sheet, tube side
    z_sheet_lo = float(cen_lo[2]) - sigma
    z_sheet_hi = float(cen_hi[2]) + sigma

    raw_sheet, _rim = _sheet(sx, sy)
    half_x = float(np.ptp(raw_sheet.coords[:, 0])) / 2.0
    half_y = float(np.ptp(raw_sheet.coords[:, 1])) / 2.0
    for name, rim, cen in (("low", rim_lo, cen_lo), ("high", rim_hi, cen_hi)):
        rp = tc[list(rim.atoms)]
        need = float(np.linalg.norm(rp[:, :2] - cen[:2], axis=1).max()) + _MARGIN
        if need > min(half_x, half_y):
            raise ValueError(
                f"reservoir sheet {sheet} too small for the {name} mouth: "
                f"needs half-extent >= {need:.2f} A, has "
                f"{min(half_x, half_y):.2f} A"
            )

    def place_sheet(z: float, cen3: np.ndarray) -> Block:
        c = np.asarray(raw_sheet.coords, dtype=float).copy()
        c[:, :2] += cen3[:2] - c[:, :2].mean(axis=0)
        c[:, 2] = z
        return replace(raw_sheet, coords=c)

    sheet_lo = place_sheet(z_sheet_lo, cen_lo)
    sheet_hi = place_sheet(z_sheet_hi, cen_hi)

    # shared twist about the tube axis: evaluate both ends per angle
    tube_cen = tc.mean(axis=0)
    best: tuple[float, float, np.ndarray] | None = None
    for tw in _TWISTS:
        tp = twist_coords(tc, tw, axis, tube_cen)
        ga = scan_opening(sheet_lo, rim_lo, tp)
        gb = scan_opening(sheet_hi, rim_hi, tp)
        if ga is None or gb is None:
            continue
        cost = ga[0] + gb[0]
        if best is None or cost < best[0]:
            best = (cost, float(tw), tp)
    if best is None:
        raise ValueError(
            "tunnel reservoirs: no shared twist seats both ends "
            "(boundary cycles never matched the rims)"
        )
    _cost, twist, placed = best

    ga = scan_opening(sheet_lo, rim_lo, placed)
    gb = scan_opening(sheet_hi, rim_hi, placed)
    assert ga is not None and gb is not None
    cut_lo, op_lo = apply_opening(sheet_lo, tblk, rim_lo, placed, ga[2])
    cut_hi, op_hi = apply_opening(sheet_hi, tblk, rim_hi, placed, gb[2])

    ns_a, nt, ns_b = len(cut_lo.elements), len(tblk.elements), len(cut_hi.elements)
    off_t, off_b = ns_a, ns_a + nt

    def flat_faces(op: Opening, off_s: int) -> tuple[tuple[int, ...], ...]:
        return tuple(
            tuple(a + (off_s if side == "s" else off_t) for side, a in f)
            for f in op.faces
        )

    faces_lo = flat_faces(op_lo, 0)
    faces_hi = flat_faces(op_hi, off_b)
    fused_lo = tuple((h, r + off_t) for h, r in op_lo.fused)
    fused_hi = tuple((h + off_b, r + off_t) for h, r in op_hi.fused)

    def junction(
        op: Opening,
        fused: tuple[tuple[int, int], ...],
        faces: tuple[tuple[int, ...], ...],
        off_s: int,
    ) -> Junction:
        zone = {a for f in faces for a in f}
        zone |= {a for p in fused for a in p}
        ub_s = tuple(sorted(a + off_s for a in op.unbonded_sheet))
        ub_t = tuple(sorted(a + off_t for a in op.unbonded_rim))
        zone |= set(ub_s) | set(ub_t)
        return Junction(
            fused=fused,
            faces=faces,
            unbonded_sheet=ub_s,
            unbonded_tube=ub_t,
            atoms=tuple(sorted(zone)),
        )

    j_lo = junction(op_lo, fused_lo, faces_lo, 0)
    j_hi = junction(op_hi, fused_hi, faces_hi, off_b)

    bonds = (
        list(cut_lo.bonds)
        + [(i + off_t, j + off_t, o) for i, j, o in tblk.bonds]
        + [(i + off_b, j + off_b, o) for i, j, o in cut_hi.bonds]
        + [(h, r, 1) for h, r in fused_lo]
        + [(h, r, 1) for h, r in fused_hi]
    )
    rings = (
        list(cut_lo.rings)
        + [tuple(a + off_t for a in g) for g in tblk.rings]
        + [tuple(a + off_b for a in g) for g in cut_hi.rings]
        + list(faces_lo)
        + list(faces_hi)
    )
    pos = np.zeros((ns_a + nt + ns_b, 3))
    pos[:ns_a] = np.asarray(cut_lo.coords)
    pos[off_t : off_t + nt] = placed
    pos[off_b:] = np.asarray(cut_hi.coords)

    def paths(inst: str, start: int, count: int) -> tuple[Atom, ...]:
        return tuple(
            Atom(AtomPath(inst, Site(k, 0, 0)), start + k, "C", "sp2", inst)
            for k in range(count)
        )

    atoms = paths("sa", 0, ns_a) + paths("tb", off_t, nt) + paths("sb", off_b, ns_b)
    findings: list[Finding] = []
    for tag, op in (("low", op_lo), ("high", op_hi)):
        census = Counter(len(f) for f in op.faces)
        findings.append(
            Finding(
                code="opening.defects",
                severity=Severity.INFO,
                message=(
                    f"{tag} mouth: {len(op.fused)} fused pairs, defect ring "
                    + ", ".join(f"{k}-ring x{v}" for k, v in sorted(census.items()))
                ),
                where=tag,
                data=(
                    ("pairs", len(op.fused)),
                    ("faces", len(op.faces)),
                    ("census", dict(sorted(census.items()))),
                    ("unbonded_sheet", len(op.unbonded_sheet)),
                    ("unbonded_rim", len(op.unbonded_rim)),
                    ("removed", len(op.removed)),
                ),
            )
        )
        if len(op.faces) < len(op.fused):
            findings.append(
                Finding(
                    code="opening.face.open",
                    severity=Severity.WARN,
                    message=(
                        f"{tag} mouth: {len(op.fused) - len(op.faces)} seam "
                        "faces could not mint a closed arc"
                    ),
                    where=tag,
                )
            )
    # relaxation at a junction decays within ~10-12 A (4-5 rings): two
    # mouths closer than _MIN_JUNCTION_SEP_A couple, so report it
    sep = float(
        np.linalg.norm(
            pos[list(j_lo.atoms)][:, None] - pos[list(j_hi.atoms)][None],
            axis=2,
        ).min()
    )
    findings.append(
        Finding(
            code="junction.spacing",
            severity=Severity.WARN if sep < _MIN_JUNCTION_SEP_A else Severity.INFO,
            message=(
                f"mouths {sep:.1f} A apart"
                + (
                    f" — below the {_MIN_JUNCTION_SEP_A:g} A relaxation-decay "
                    "margin (junction strain fields couple; relax together)"
                    if sep < _MIN_JUNCTION_SEP_A
                    else f" — clear of the {_MIN_JUNCTION_SEP_A:g} A "
                    "relaxation-decay margin"
                )
            ),
            where="tube",
            data=(
                ("separation_A", sep),
                ("margin_A", _MIN_JUNCTION_SEP_A),
            ),
        )
    )
    net = Net(
        atoms=atoms,
        bonds=tuple(bonds),
        rings=tuple(rings),
        ports=(),
        regions=(
            ("sheet_lo", tuple(range(ns_a))),
            ("tube", tuple(range(off_t, off_t + nt))),
            ("sheet_hi", tuple(range(off_b, off_b + ns_b))),
            ("junction_lo", j_lo.atoms),
            ("junction_hi", j_hi.atoms),
        ),
        report=Report(tuple(findings)),
        spec=Spec(),
        lattice=lattice,
        seed3=tuple(map(tuple, pos)),
        seed_kind="mixed",
    )
    return net, Tunnel(
        kind=kind,
        sheet_size=(sx, sy),
        sheet_lo=tuple(range(ns_a)),
        sheet_hi=tuple(range(off_b, off_b + ns_b)),
        tube_atoms=tuple(range(off_t, off_t + nt)),
        junctions=(j_lo, j_hi),
        coords=pos,
        sheet_plane_z=(z_sheet_lo, z_sheet_hi),
        twist_deg=twist,
        strip_atoms=tuple(tuple(a + off_t for a in s) for s in strips0),
        seam_atoms=tuple(tuple(a + off_t for a in s) for s in seams0),
    )
