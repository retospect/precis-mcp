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

import math
from collections import Counter
from dataclasses import dataclass, replace

import numpy as np

from .build import Atom, Net, build
from .ids import AtomPath
from .join import Block, block_from_net
from .lattice import Lattice, Site
from .opening import (
    _TIE_COST,
    _TIE_POS,
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
from .stick import stick_info
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
    #: the strain read's label (``low``/``high`` mouths,
    #: ``reducer:<w>-<n>:outer|inner`` step seams)
    name: str = ""


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
    #: stepped tunnels: which segment each strip/seam tuple belongs to,
    #: parallel to strip_atoms / seam_atoms (empty for a single segment)
    strip_segment: tuple[int, ...] = ()
    seam_segment: tuple[int, ...] = ()
    #: stepped tunnels: the washer seams between segments, named
    #: ``reducer:<wide>-<narrow>:outer|inner`` like the grammar reducer
    reducers: tuple[Junction, ...] = ()
    #: stepped tunnels: composite ordinals per segment (parallel to the
    #: authored segment list)
    segment_atoms: tuple[tuple[int, ...], ...] = ()


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
        return (lo, hi) if round(float(zlo - zhi), _TIE_POS) <= 0 else (hi, lo)
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
        if round(need - min(half_x, half_y), _TIE_POS) > 0:
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
        cost = round(ga[0] + gb[0], _TIE_COST)
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
    sep_close = round(sep - _MIN_JUNCTION_SEP_A, _TIE_POS) < 0
    findings.append(
        Finding(
            code="junction.spacing",
            severity=Severity.WARN if sep_close else Severity.INFO,
            message=(
                f"mouths {sep:.1f} A apart"
                + (
                    f" — below the {_MIN_JUNCTION_SEP_A:g} A relaxation-decay "
                    "margin (junction strain fields couple; relax together)"
                    if sep_close
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


#: the wide segment's deepest star notch (its rib tips) must clear the
#: narrow segment's outermost rim radius by this margin, else the
#: washer's inner hole and outer edge meet at a notch corridor and the
#: step is refused (``step.throat``) rather than stitched wrong
_STEP_THROAT_A = 4.5


def _rim_radii(coords: np.ndarray, rim: RimLoop) -> tuple[float, float]:
    """(min, max) radial distance of the rim's dangling atoms off the axis."""
    r = np.linalg.norm(np.asarray(coords)[list(rim.atoms), :2], axis=1)
    return float(r.min()), float(r.max())


def _cone_block(p: int, r_need: float, lattice: Lattice) -> Block:
    """Mint + fold the p-pentagon graphene cone reaching ``r_need`` in xy.

    The grammar's ``cone(p, rad=)`` mints the net with
    ``build._cone_defects`` (p wedge excisions equally spaced in azimuth);
    the ``_cone_seed`` bump only breaks planarity, so one untethered stick
    pass folds the net onto its true cone surface -- all bonds back to
    sigma -- which is the surface the rims stitch onto.  ``rad`` is sized
    so the folded cone's xy reach clears ``r_need`` plus margin for the
    scale scan."""
    a = lattice.a
    rad = max(10, math.ceil((1.8 * r_need + _MARGIN) / (0.9 * a)) + 2)
    net = build(f"hexfold 0.2\ncx: cone({p}, rad={rad})\n")
    if not net.report.ok:
        raise ValueError(f"cone({p}, rad={rad}): {net.report.findings[0].message}")
    coords, _force = stick_info(net)
    return block_from_net(net, np.asarray(coords, dtype=float))


def _cone_step(
    p: int,
    blks: list[Block],
    w_i: int,
    n_i: int,
    w_rim: RimLoop,
    n_rim: RimLoop,
    w_pos: np.ndarray,
    n_pos: np.ndarray,
    seams_w: tuple[tuple[int, ...], ...],
    step_i: int,
    lattice: Lattice,
) -> tuple[Block, Opening, Opening, float]:
    """One ``step: cone`` transition: a truncated cone between the wide
    and narrow rims, stitched like the washer (docs/backlog/
    hexfold-nanoreactor-tube.md, H2).

    The cone carries ``p = rails`` pentagons (the star outlines are
    N-fold, so the disclinations sit one per seam azimuth of the wide
    segment).  Its block replaces the washer sheet in the same
    two-opening flow: the narrow rim stitches into a hole cut toward the
    apex, the wide rim onto the cone's outer edge; both cuts land at the
    slant distances whose circumferences the scale scan matches to the
    rail-star outlines.  The rim planes seat one sigma off the mean cone
    surface under their rim atoms -- the seed seam bonds run long at the
    star notches (the rim is flat, the surface sloped) and the tethered
    relax closes them."""
    sigma = lattice.sigma_A
    w_star = np.linalg.norm(w_pos[list(w_rim.atoms), :2], axis=1)
    n_star = np.linalg.norm(n_pos[list(n_rim.atoms), :2], axis=1)
    cone = _cone_block(p, float(w_star.max()) * 1.45, lattice)
    cpos = np.asarray(cone.coords, dtype=float)

    # apex points toward the narrow segment (later in the +z stack);
    # each rim's dangling plane seats one sigma off the surface on the
    # seam side
    w_z = float(w_pos[list(w_rim.atoms), 2].mean())
    n_z_now = float(n_pos[list(n_rim.atoms), 2].mean())
    dirn = 1.0 if n_i > w_i else -1.0
    if dirn < 0:
        # a half-turn about x, not a mirror: the lattice keeps its hand
        cpos[:, 1] = -cpos[:, 1]
        cpos[:, 2] = -cpos[:, 2]

    # rotate so the pentagons sit on the wide segment's seam azimuths
    cnet_rings = [r for r in cone.rings if len(r) == 5]
    pen_cen = np.array([cpos[list(r)].mean(axis=0) for r in cnet_rings])
    pen_az = np.sort(np.degrees(np.arctan2(pen_cen[:, 1], pen_cen[:, 0])) % 360.0)
    seam_az = np.sort(
        np.degrees(
            np.arctan2(
                [w_pos[list(s)][:, 1].mean() for s in seams_w],
                [w_pos[list(s)][:, 0].mean() for s in seams_w],
            )
        )
        % 360.0
    )
    if len(pen_az) == len(seam_az) and len(pen_az):
        rot_deg = float(np.median(seam_az - pen_az))
        rot = math.radians(rot_deg)
        rm = np.array([[math.cos(rot), -math.sin(rot)], [math.sin(rot), math.cos(rot)]])
        cpos[:, :2] = cpos[:, :2] @ rm.T

    # vertical seat: the cone surface under each rim's atoms lies one
    # sigma off that rim's plane (the washer's seat)
    r_c = np.linalg.norm(cpos[:, :2], axis=1)
    z_col = cpos[:, 2].copy()

    def surf_z(r: float) -> float:
        sel = np.round(np.abs(r_c - r) - 0.8, _TIE_POS) < 0
        return float(np.median(z_col[sel])) if sel.any() else np.nan

    surf_w = [surf_z(float(r)) for r in w_star]
    if any(math.isnan(z) for z in surf_w):
        raise ValueError(
            f"step {step_i}: the p={p} cone does not reach the wide rim's "
            f"{float(w_star.max()):.2f} A outline"
        )
    z_shift = w_z + sigma * dirn - float(np.mean(surf_w))
    surf_n = [surf_z(float(r)) for r in n_star]
    z_n = float(np.mean(surf_n)) + z_shift + sigma * dirn
    cpos[:, 2] += z_shift
    cone = replace(cone, coords=cpos)
    n_pos = n_pos.copy()
    n_pos[:, 2] += z_n - n_z_now

    g_in = scan_opening(cone, n_rim, n_pos)
    if g_in is None:
        raise ValueError(
            f"step {step_i}: no cone cut leaves a boundary of "
            f"{len(n_rim.atoms)} bondable atoms for the narrow rim"
        )
    cut1, op_in = apply_opening(cone, blks[n_i], n_rim, n_pos, g_in[2])
    g_out = scan_opening(cut1, w_rim, w_pos, outside=True)
    if g_out is None:
        raise ValueError(
            f"step {step_i}: no cone cut leaves an outer boundary of "
            f"{len(w_rim.atoms)} bondable atoms for the wide rim"
        )
    cut2, op_out = apply_opening(cut1, blks[w_i], w_rim, w_pos, g_out[2], outside=True)
    # the outer cut may not touch the inner stitch's cone-side atoms
    keep_map = np.full(len(cut1.elements), -1, dtype=np.int64)
    survivors = sorted(set(range(len(cut1.elements))) - set(op_out.removed))
    keep_map[np.asarray(survivors)] = np.arange(len(survivors))
    stitched = {h for h, _r in op_in.fused} | {
        a for f in op_in.faces for side, a in f if side == "s"
    }
    lost = stitched & set(op_out.removed)
    if lost:
        raise ValueError(
            f"step {step_i}: the wide rim's cut removed {len(lost)} "
            "atom(s) of the narrow stitch — the cone frustum pinched to "
            "nothing between the rims (widen the step)"
        )
    op_in2 = replace(
        op_in,
        hole_walk=tuple(int(keep_map[a]) for a in op_in.hole_walk),
        hole_bondable=tuple(int(keep_map[a]) for a in op_in.hole_bondable),
        fused=tuple((int(keep_map[h]), r) for h, r in op_in.fused),
        faces=tuple(
            tuple(
                (side, int(keep_map[a])) if side == "s" else (side, a) for side, a in f
            )
            for f in op_in.faces
        ),
        unbonded_sheet=tuple(int(keep_map[a]) for a in op_in.unbonded_sheet),
    )
    return cut2, op_in2, op_out, z_n


def tunnel_steps(
    segments: tuple[tuple, ...],
    sheet: tuple[int, int],
    *,
    kinds: tuple[str, ...] | None = None,
    step: str = "washer",
) -> tuple[Net, Tunnel]:
    """Assemble ``sheet | seg | washer | seg | ... | sheet``.

    The H2 radius-step tunnel (docs/backlog/hexfold-nanoreactor-tube.md):
    ``segments`` is a list of per-segment specs, each resolved by
    :func:`_tube_block` -- ``(n, periods)`` armchair or ``(rows, periods,
    strip_rows, rails, pores)`` rail (``kinds`` defaults to all-rail).  A
    step between two ends is the reducer washer of ``fuse{reducer}``'s
    role -- a flat annulus minted by the opening solver rather than a
    ``cap`` registry, because a rail end is a star, not a grammar rim:
    the narrow end stitches into a hole cut inside the sheet, the wide
    end onto the sheet's outer edge, defect rings minted where the
    boundaries disagree (``opening.face.open`` reports what cannot
    close -- never faked).

    Refusals, by name: a segment list shorter than two; rail segments
    with different ``rails`` counts (the seam lines would not register
    across the step); a narrow segment whose rim pokes past the wide
    segment's rib tips (``step.throat``); a washer sheet the annulus
    does not fit; a cut that destroys a stitch made earlier
    (``step.washer.collide``).
    """
    if len(segments) < 2:
        raise ValueError("tunnel steps needs at least two segments")
    kinds = kinds or ("rail",) * len(segments)
    if len(kinds) != len(segments):
        raise ValueError("tunnel steps kinds must match the segment count")
    if step not in ("washer", "cone"):
        raise ValueError(f"tunnel steps step must be 'washer' or 'cone', got {step!r}")
    rails_of = [
        spec[3] for kind, spec in zip(kinds, segments, strict=True) if kind == "rail"
    ]
    if len(set(rails_of)) > 1:
        raise ValueError(
            f"stepped rail segments must share one rail count; got {rails_of}"
        )
    if step == "cone" and rails_of and rails_of[0] >= 6:
        raise ValueError(
            "step.cone.symmetry: a cone transition carries p = rails "
            f"pentagons, one per seam azimuth; p = {rails_of[0]} is flat, "
            "no cone exists"
        )
    sx, sy = sheet
    lattice = Lattice()
    sigma = lattice.sigma_A
    axis = np.array([0.0, 0.0, 1.0])

    blks: list[Block] = []
    strips0: list[tuple[tuple[int, ...], ...]] = []
    seams0: list[tuple[tuple[int, ...], ...]] = []
    for kind, spec in zip(kinds, segments, strict=True):
        blk, _net, st, se = _tube_block(kind, spec)
        blks.append(blk)
        strips0.append(st)
        seams0.append(se)

    # stack along +z: seg0's low dangling plane at 0; each step's washer
    # plane sits sigma off the facing rims
    placed: list[np.ndarray] = []
    rims: list[tuple[RimLoop, RimLoop]] = []
    z_lo = 0.0
    washers: list[tuple[int, int, float, Block, Opening, Opening]] = []
    for i, blk in enumerate(blks):
        c = np.asarray(blk.coords, dtype=float).copy()
        lo = rim_loop_end(c, blk.bonds, axis, "low")
        hi = rim_loop_end(c, blk.bonds, axis, "high")
        c[:, 2] += z_lo - float(c[list(lo.atoms), 2].mean())
        placed.append(c)
        rims.append((lo, hi))
        if i + 1 == len(blks):
            break
        # the washer plane hangs sigma past this segment's high rim
        z_p = float(c[list(hi.atoms), 2].mean()) + sigma
        nxt = np.asarray(blks[i + 1].coords, dtype=float)
        lo_next = rim_loop_end(nxt, blks[i + 1].bonds, axis, "low")
        # which side is wider: compare rim spans (the star's deepest notch
        # is the rib tips; a wider segment's rim reaches farther out)
        w_i, n_i = (i, i + 1)
        w_rim, n_rim = hi, lo_next
        w_pos, n_pos = c, nxt
        w_min, w_max = _rim_radii(c, w_rim)
        n_min, n_max = _rim_radii(nxt, n_rim)
        if round(n_min - w_min, _TIE_POS) > 0:
            # the "next" segment is the wider one (an upward step)
            w_i, n_i = n_i, i
            w_rim, n_rim = lo_next, hi
            w_pos, n_pos = nxt, c
            w_min, w_max, n_min, n_max = n_min, n_max, w_min, w_max
        if step == "cone":
            nxt = nxt.copy()
            nxt[:, 2] -= float(nxt[list(lo_next.atoms), 2].mean())
            n_pos = nxt if n_i == i + 1 else c
            w_pos = nxt if w_i == i + 1 else c
            cut2, op_in2, op_out, z_n = _cone_step(
                rails_of[0],
                blks,
                w_i,
                n_i,
                w_rim,
                n_rim,
                w_pos,
                n_pos,
                seams0[w_i],
                i,
                lattice,
            )
            if n_i == i + 1:
                # downward: the wide rim (this segment) is already placed,
                # the narrow segment's low rim lands at the seated z_n
                z_lo = z_n
            else:
                # upward: the cone was seated on the wide (next) segment
                # with its low rim at 0 and the narrow rim at z_n; slide
                # that frame so the narrow rim lands on this segment's
                # placed high rim
                delta = float(c[list(hi.atoms), 2].mean()) - z_n
                cc2 = np.asarray(cut2.coords, dtype=float).copy()
                cc2[:, 2] += delta
                cut2 = replace(cut2, coords=cc2)
                z_lo = delta
            washers.append((w_i, n_i, z_lo, cut2, op_in2, op_out))
            continue
        if round(n_max + _STEP_THROAT_A - w_min, _TIE_POS) > 0:
            raise ValueError(
                f"step {i}: the narrower segment's rim reaches "
                f"{n_max:.2f} A off the axis but the wider segment's star "
                f"notches dip to {w_min:.2f} A — the washer annulus would "
                f"pinch shut (need {_STEP_THROAT_A:g} A clear); shrink the "
                "narrow segment's wall or the wide one's ribs"
            )
        nxt = nxt.copy()
        nxt[:, 2] += z_p + sigma - float(nxt[list(lo_next.atoms), 2].mean())
        n_pos = nxt if n_i == i + 1 else c
        w_pos = nxt if w_i == i + 1 else c
        z_lo = z_p + sigma
        # washer sheet: big enough for the wide rim plus margin, and in a
        # lattice registry where both scans close their boundary cycles --
        # a size that fits can still fail the pairing scan, so size is a
        # scan, not a lookup
        need = w_max + _MARGIN
        got: tuple[Block, Opening, Block, Opening] | None = None
        for wsize in range(int(need / 0.9) + 8, int(need / 0.9) + 60):
            cand, _rim = _sheet(wsize, wsize)
            if (
                round(
                    float(np.ptp(cand.coords[:, :2], axis=0).min()) / 2.0 - need,
                    _TIE_POS,
                )
                < 0
            ):
                continue
            cc = np.asarray(cand.coords, dtype=float).copy()
            cc[:, :2] -= cc[:, :2].mean(axis=0)
            cc[:, 2] = z_p
            w_sheet = replace(cand, coords=cc)
            g_in = scan_opening(w_sheet, n_rim, n_pos)
            if g_in is None:
                continue
            cut1, op_in = apply_opening(w_sheet, blks[n_i], n_rim, n_pos, g_in[2])
            g_out = scan_opening(cut1, w_rim, w_pos, outside=True)
            if g_out is None:
                continue
            cut2, op_out = apply_opening(
                cut1, blks[w_i], w_rim, w_pos, g_out[2], outside=True
            )
            got = (cut1, op_in, cut2, op_out)
            break
        if got is None:
            raise ValueError(
                f"step {i}: no washer sheet ({need:.1f} A needed) leaves a "
                f"hole boundary of {len(n_rim.atoms)} and an outer boundary "
                f"of {len(w_rim.atoms)} bondable atoms in one registry"
            )
        cut1, op_in, cut2, op_out = got
        # the outer cut may not touch the inner stitch's sheet-side atoms
        keep_map = np.full(len(cut1.elements), -1, dtype=np.int64)
        survivors = sorted(set(range(len(cut1.elements))) - set(op_out.removed))
        keep_map[np.asarray(survivors)] = np.arange(len(survivors))
        stitched = {h for h, _r in op_in.fused} | {
            a for f in op_in.faces for side, a in f if side == "s"
        }
        lost = stitched & set(op_out.removed)
        if lost:
            raise ValueError(
                f"step {i}: the wide rim's cut removed {len(lost)} atom(s) of "
                "the narrow stitch — the annulus pinched to nothing between "
                "the star notches and the hole (widen the step)"
            )
        # the inner opening's sheet-side ordinals move onto cut2's indexing
        op_in2 = replace(
            op_in,
            hole_walk=tuple(int(keep_map[a]) for a in op_in.hole_walk),
            hole_bondable=tuple(int(keep_map[a]) for a in op_in.hole_bondable),
            fused=tuple((int(keep_map[h]), r) for h, r in op_in.fused),
            faces=tuple(
                tuple(
                    (side, int(keep_map[a])) if side == "s" else (side, a)
                    for side, a in f
                )
                for f in op_in.faces
            ),
            unbonded_sheet=tuple(int(keep_map[a]) for a in op_in.unbonded_sheet),
        )
        washers.append((w_i, n_i, z_p, cut2, op_in2, op_out))
    # ---- assemble the tube stack: segments in order, then washers ----
    offs: list[int] = []
    off = 0
    for blk in blks:
        offs.append(off)
        off += len(blk.elements)
    w_offs = []
    for _w_i, _n_i, _z_p, cut2, _op_in, _op_out in washers:
        w_offs.append(off)
        off += len(cut2.elements)
    total = off
    pos = np.zeros((total, 3))
    for blk_o, c in zip(offs, placed, strict=True):
        pos[blk_o : blk_o + len(c)] = c
    for w_o, (w_i, n_i, _z, cut2, _oi, _oo) in zip(w_offs, washers, strict=True):
        pos[w_o : w_o + len(cut2.elements)] = np.asarray(cut2.coords)
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for blk_o, blk in zip(offs, blks, strict=True):
        bonds.extend((i + blk_o, j + blk_o, o) for i, j, o in blk.bonds)
        rings.extend(tuple(a + blk_o for a in r) for r in blk.rings)
    reducer_junctions: list[Junction] = []
    for si, ((w_i, n_i, _z, cut2, op_in, op_out), w_o) in enumerate(
        zip(washers, w_offs, strict=True)
    ):
        bonds.extend((i + w_o, j + w_o, o) for i, j, o in cut2.bonds)
        rings.extend(tuple(a + w_o for a in r) for r in cut2.rings)
        for tag, op, rim_i in (
            ("outer", op_out, w_i),
            ("inner", op_in, n_i),
        ):
            seg_off = offs[rim_i]
            fused = tuple((h + w_o, r + seg_off) for h, r in op.fused)
            faces = tuple(
                tuple(a + (w_o if side == "s" else seg_off) for side, a in f)
                for f in op.faces
            )
            bonds.extend((h_, r_, 1) for h_, r_ in fused)
            rings.extend(faces)
            ub_s = tuple(sorted(a + w_o for a in op.unbonded_sheet))
            ub_t = tuple(sorted(a + seg_off for a in op.unbonded_rim))
            zone = (
                {a for p in fused for a in p}
                | {a for f in faces for a in f}
                | set(ub_s)
                | set(ub_t)
            )
            reducer_junctions.append(
                Junction(
                    fused=fused,
                    faces=faces,
                    unbonded_sheet=ub_s,
                    unbonded_tube=ub_t,
                    atoms=tuple(sorted(zone)),
                    name=f"reducer:seg{w_i}->seg{n_i}:{tag}",
                )
            )
    tube_blk = Block(
        elements=tuple(e for blk in blks for e in blk.elements)
        + tuple(e for _w in washers for e in _w[3].elements),
        coords=pos,
        bonds=tuple(bonds),
        rings=tuple(rings),
        ports={},
        sigma=sigma,
    )

    def _off_rim(rim: RimLoop, off_: int) -> RimLoop:
        return RimLoop(
            atoms=tuple(a + off_ for a in rim.atoms),
            band=frozenset(a + off_ for a in rim.band),
            walk=None if rim.walk is None else tuple(a + off_ for a in rim.walk),
        )

    rim_lo = _off_rim(rims[0][0], offs[0])
    rim_hi = _off_rim(rims[-1][1], offs[-1])

    tc = np.asarray(tube_blk.coords, dtype=float)
    cen_lo = tc[list(rim_lo.atoms)].mean(axis=0)
    cen_hi = tc[list(rim_hi.atoms)].mean(axis=0)
    z_sheet_lo = float(cen_lo[2]) - sigma
    z_sheet_hi = float(cen_hi[2]) + sigma

    raw_sheet, _rim = _sheet(sx, sy)
    half_x = float(np.ptp(raw_sheet.coords[:, 0])) / 2.0
    half_y = float(np.ptp(raw_sheet.coords[:, 1])) / 2.0
    for name, rim, cen in (("low", rim_lo, cen_lo), ("high", rim_hi, cen_hi)):
        rp = tc[list(rim.atoms)]
        need = float(np.linalg.norm(rp[:, :2] - cen[:2], axis=1).max()) + _MARGIN
        if round(need - min(half_x, half_y), _TIE_POS) > 0:
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

    tube_cen = tc.mean(axis=0)
    best: tuple[float, float, np.ndarray] | None = None
    for tw in _TWISTS:
        tp = twist_coords(tc, tw, axis, tube_cen)
        ga = scan_opening(sheet_lo, rim_lo, tp)
        gb = scan_opening(sheet_hi, rim_hi, tp)
        if ga is None or gb is None:
            continue
        cost = round(ga[0] + gb[0], _TIE_COST)
        if best is None or cost < best[0]:
            best = (cost, float(tw), tp)
    if best is None:
        raise ValueError(
            "tunnel reservoirs: no shared twist seats both ends "
            "(boundary cycles never matched the rims)"
        )
    _cost, twist, placed_t = best

    ga = scan_opening(sheet_lo, rim_lo, placed_t)
    gb = scan_opening(sheet_hi, rim_hi, placed_t)
    assert ga is not None and gb is not None
    cut_lo, op_lo = apply_opening(sheet_lo, tube_blk, rim_lo, placed_t, ga[2])
    cut_hi, op_hi = apply_opening(sheet_hi, tube_blk, rim_hi, placed_t, gb[2])

    ns_a, nt, ns_b = len(cut_lo.elements), len(tube_blk.elements), len(cut_hi.elements)
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
        name: str,
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
            name=name,
        )

    j_lo = junction(op_lo, fused_lo, faces_lo, 0, "low")
    j_hi = junction(op_hi, fused_hi, faces_hi, off_b, "high")
    # reducer junctions already carry composite-tube ordinals; shift by off_t
    red_final = tuple(
        Junction(
            fused=tuple((h + off_t, r + off_t) for h, r in j.fused),
            faces=tuple(tuple(a + off_t for a in f) for f in j.faces),
            unbonded_sheet=tuple(a + off_t for a in j.unbonded_sheet),
            unbonded_tube=tuple(a + off_t for a in j.unbonded_tube),
            atoms=tuple(a + off_t for a in j.atoms),
            name=j.name,
        )
        for j in reducer_junctions
    )
    bonds_all = (
        list(cut_lo.bonds)
        + [(i + off_t, j + off_t, o) for i, j, o in tube_blk.bonds]
        + [(i + off_b, j + off_b, o) for i, j, o in cut_hi.bonds]
        + [(h, r, 1) for h, r in fused_lo]
        + [(h, r, 1) for h, r in fused_hi]
    )
    rings_all = (
        list(cut_lo.rings)
        + [tuple(a + off_t for a in g) for g in tube_blk.rings]
        + [tuple(a + off_b for a in g) for g in cut_hi.rings]
        + list(faces_lo)
        + list(faces_hi)
    )
    pos_all = np.zeros((ns_a + nt + ns_b, 3))
    pos_all[:ns_a] = np.asarray(cut_lo.coords)
    pos_all[off_t : off_t + nt] = placed_t
    pos_all[off_b:] = np.asarray(cut_hi.coords)

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
    for j in reducer_junctions:
        census = Counter(len(f) for f in j.faces)
        findings.append(
            Finding(
                code="opening.defects",
                severity=Severity.INFO,
                message=(
                    f"{j.name}: {len(j.fused)} fused pairs, defect ring "
                    + ", ".join(f"{k}-ring x{v}" for k, v in sorted(census.items()))
                ),
                where=j.name,
                data=(
                    ("pairs", len(j.fused)),
                    ("faces", len(j.faces)),
                    ("census", dict(sorted(census.items()))),
                ),
            )
        )
        if len(j.faces) < len(j.fused):
            findings.append(
                Finding(
                    code="opening.face.open",
                    severity=Severity.WARN,
                    message=(
                        f"{j.name}: {len(j.fused) - len(j.faces)} seam faces "
                        "could not mint a closed arc"
                    ),
                    where=j.name,
                )
            )
    seg_atoms = tuple(
        tuple(range(offs[i], offs[i] + len(blks[i].elements))) for i in range(len(blks))
    )
    strip_atoms: list[tuple[int, ...]] = []
    seam_atoms: list[tuple[int, ...]] = []
    strip_seg: list[int] = []
    seam_seg: list[int] = []
    for i, (st, se) in enumerate(zip(strips0, seams0, strict=True)):
        strip_atoms.extend(tuple(a + offs[i] for a in s) for s in st)
        seam_atoms.extend(tuple(a + offs[i] for a in s) for s in se)
        strip_seg.extend([i] * len(st))
        seam_seg.extend([i] * len(se))
    regions = [
        ("sheet_lo", tuple(range(ns_a))),
        ("tube", tuple(range(off_t, off_t + nt))),
        ("sheet_hi", tuple(range(off_b, off_b + ns_b))),
        ("junction_lo", j_lo.atoms),
        ("junction_hi", j_hi.atoms),
    ]
    regions.extend(
        (f"seg{i}", tuple(a + off_t for a in s)) for i, s in enumerate(seg_atoms)
    )
    regions.extend(
        (j.name, tuple(a + off_t for a in j.atoms)) for j in reducer_junctions
    )
    net = Net(
        atoms=atoms,
        bonds=tuple(bonds_all),
        rings=tuple(rings_all),
        ports=(),
        regions=tuple(regions),
        report=Report(tuple(findings)),
        spec=Spec(),
        lattice=lattice,
        seed3=tuple(map(tuple, pos_all)),
        seed_kind="mixed",
    )
    return net, Tunnel(
        kind="rail" if any(k == "rail" for k in kinds) else "armchair",
        sheet_size=(sx, sy),
        sheet_lo=tuple(range(ns_a)),
        sheet_hi=tuple(range(off_b, off_b + ns_b)),
        tube_atoms=tuple(range(off_t, off_t + nt)),
        junctions=(j_lo, j_hi),
        coords=pos_all,
        sheet_plane_z=(z_sheet_lo, z_sheet_hi),
        twist_deg=twist,
        strip_atoms=tuple(tuple(a + off_t for a in s) for s in strip_atoms),
        seam_atoms=tuple(tuple(a + off_t for a in s) for s in seam_atoms),
        strip_segment=tuple(strip_seg),
        seam_segment=tuple(seam_seg),
        reducers=red_final,
        segment_atoms=tuple(tuple(a + off_t for a in s) for s in seg_atoms),
    )
