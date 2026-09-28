"""Helix geometry — the bridge from a stored ``chain`` record to the
:mod:`precis_chain` kernel's arrays.

One function matters: :func:`helix_geometry` turns a helix block's
declaration into a :class:`HelixGeometry` — the centre-line
:class:`~precis_chain.path.Path`, the per-unit origins and frames
(:func:`precis_chain.fibre.unit_frames`), and the numbers the checks need
(the register-retuned motif, the bend limit, the clash tolerance). The
``layout_chain`` op, the pure DRC pass and the ``view='chain'`` renderer
all read it, so the geometry is derived once per block per call and in one
place.

**Frames.** A lattice helix runs along world ``+z`` from its site, seeded
with ``r0 = +x``, so its unit frames' normal is world ``+x`` and the
kernel's azimuth convention (0 along the normal) coincides with the
in-plane angle :mod:`precis_se.chain.nucleic`'s lattice directions are
quoted in. That coincidence is what makes "the backbone faces its
neighbour" a comparison between two numbers in the same frame; a
free-waypoint helix has no lattice and makes no such claim.

**Segments tile the unit range exactly** (:func:`segment_ranges`) — the
realizer seam ``se-nucleic-realize-export`` needs. That is why the split
is by *unit count* rather than
:func:`precis_chain.envelope.capsules_along`'s uniform arc length: the
kernel's cuts land wherever the arc length says, which is between units,
and a segment whose declared ``[start, end]`` did not match its own
envelope would be worse than no seam at all. The capsule itself is still
the kernel's (:class:`~precis_chain.envelope.Capsule` +
:func:`~precis_chain.envelope.capsule_pose`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis_chain.envelope import Capsule, capsule_pose
from precis_chain.fibre import UnitFrames, backbone_exit, unit_frames
from precis_chain.motif import Motif, units_for_length
from precis_chain.path import Path, polyline
from precis_se.chain import nucleic
from precis_se.chain.vocab import (
    DEFAULT_SEGMENT_UNITS,
    HELIX_ROLE,
    ChainError,
    chain_role,
)


@dataclass(frozen=True)
class HelixGeometry:
    """One helix's realised geometry and the limits it is checked against.

    ``motif`` is the **register-retuned** motif
    (:func:`precis_se.chain.nucleic.lattice_motif`) — the twist the
    lattice's crossovers actually hold the helix at — and ``base_motif``
    the unconstrained one, kept so a report can name both. ``units``
    carries one origin and one frame per unit; ``lattice`` is the register
    lattice's name, or ``None`` for a free centre line.

    ``min_bend_radius_m`` is the authored limit when there is one and
    :data:`precis_se.chain.nucleic.DEFAULT_MIN_BEND_RADIUS_M` otherwise —
    ``bend_authored`` says which, because the finding's severity depends on
    it (authored → error, coded default → warn).
    """

    name: str
    nucleic: str
    motif: Motif
    base_motif: Motif
    lattice: str | None
    n_units: int
    phase0: float
    path: Path
    units: UnitFrames
    min_bend_radius_m: float
    bend_authored: bool
    min_gap_m: float
    gap_authored: bool

    def origin(self, offset: int) -> np.ndarray:
        """Unit ``offset``'s position on the axis, metres."""
        return np.asarray(self.units.origins[offset], dtype=float)

    def exit(self, offset: int, forward: bool) -> np.ndarray:
        """Where the ``forward``/reverse strand's backbone sits at unit
        ``offset``, metres — the point a loop has to reach
        (:func:`precis_chain.fibre.backbone_exit`, with ``origins=`` so the
        answer is in world coordinates)."""
        return np.asarray(
            backbone_exit(
                self.units.frames[offset],
                self.motif,
                nucleic.STRAND_AZIMUTH_RAD[bool(forward)],
                self.units.origins[offset],
            ),
            dtype=float,
        )

    def azimuth(self, offset: int, forward: bool) -> float:
        """The backbone's accumulated azimuth at unit ``offset``, radians —
        ``phase0 + offset * twist`` plus the strand's own offset, wrapped to
        ``[-pi, pi)``. Reported by ``view='chain'``; the reach check uses
        :meth:`exit` rather than this, since an angle alone says nothing
        about distance."""
        raw = (
            self.phase0
            + offset * self.motif.twist
            + nucleic.STRAND_AZIMUTH_RAD[bool(forward)]
        )
        return float((raw + math.pi) % (2.0 * math.pi) - math.pi)


def _helix_path(record: dict[str, Any], motif: Motif, n_units: int) -> Path:
    """The centre line: a straight run along ``+z`` from the lattice site,
    or the authored waypoints.

    A one-unit helix still needs two path samples (a path has no tangent
    otherwise), so the straight run is at least one rise long — the extra
    rise is past the last unit's origin and no unit is placed on it.
    """
    path_record = record.get("path") or {}
    site = path_record.get("lattice")
    if site is not None:
        x, y = nucleic.site_position(site["kind"], int(site["row"]), int(site["col"]))
        length = max(n_units - 1, 1) * motif.rise
        return polyline(np.array([[x, y, 0.0], [x, y, length]]))
    waypoints = path_record.get("waypoints_m") or []
    return polyline(np.array([[float(v) for v in p] for p in waypoints], dtype=float))


def helix_geometry(node: Any) -> HelixGeometry:
    """Realise a helix block's geometry. Raises :class:`ChainError` when the
    block is not a helix or its record cannot produce geometry (a path too
    short for its ``n_units`` — the kernel's own refusal, re-raised with
    the block named)."""
    if chain_role(node) != HELIX_ROLE:
        raise ChainError(f"block {getattr(node, 'name', '?')!r} is not a helix")
    record = dict(node.chain)
    nucleic_name = str(record.get("nucleic") or "DNA")
    base_motif = nucleic.MOTIFS[
        str(record.get("motif") or nucleic.MOTIF_FOR_NUCLEIC[nucleic_name])
    ]
    lattice = (record.get("register") or {}).get("lattice")
    site = (record.get("path") or {}).get("lattice")
    if lattice is None and site is not None:
        lattice = site["kind"]
    motif = nucleic.lattice_motif(base_motif, lattice)
    n_units = int(record["n_units"])
    phase0 = float(record.get("phase0") or 0.0)
    path = _helix_path(record, motif, n_units)
    try:
        units = unit_frames(path, motif, n_units, phase0, r0=np.array([1.0, 0.0, 0.0]))
    except ValueError as exc:
        raise ChainError(f"helix {node.name!r}: {exc}") from exc
    bend_authored = record.get("min_bend_radius_m") is not None
    gap_authored = record.get("min_gap_m") is not None
    return HelixGeometry(
        name=str(node.name),
        nucleic=nucleic_name,
        motif=motif,
        base_motif=base_motif,
        lattice=lattice,
        n_units=n_units,
        phase0=phase0,
        path=path,
        units=units,
        min_bend_radius_m=(
            float(record["min_bend_radius_m"])
            if bend_authored
            else base_motif.min_bend_radius
        ),
        bend_authored=bend_authored,
        min_gap_m=(
            float(record["min_gap_m"])
            if gap_authored
            else nucleic.default_min_gap_m(base_motif)
        ),
        gap_authored=gap_authored,
    )


def units_per_segment(geom: HelixGeometry, max_seg_len_m: float | None = None) -> int:
    """How many units one ``layout_chain`` child covers.

    ``max_seg_len_m`` given → as many whole units as fit in it
    (:func:`precis_chain.motif.units_for_length`, floored at 1 — a
    ``max_seg_len`` under one rise still yields one unit per segment rather
    than an empty tiling). Otherwise **one lattice repeat** (21 units
    honeycomb, 32 square), or :data:`precis_se.chain.vocab.
    DEFAULT_SEGMENT_UNITS` for a helix with no lattice.
    """
    if max_seg_len_m is not None:
        return max(1, units_for_length(geom.motif, max_seg_len_m))
    if geom.lattice is not None:
        return nucleic.LATTICES[geom.lattice].pitch_units
    return DEFAULT_SEGMENT_UNITS


def segment_ranges(n_units: int, per_segment: int) -> list[tuple[int, int]]:
    """``[(start, end), …]`` INCLUSIVE unit ranges that tile
    ``range(n_units)`` exactly — no gap, no overlap, in order.

    The tiling is the contract (:mod:`precis_se.chain.layout`'s docstring):
    ``ranges[0][0] == 0``, ``ranges[-1][1] == n_units - 1`` and
    ``ranges[i][1] + 1 == ranges[i + 1][0]`` for every ``i``.
    """
    if n_units < 1 or per_segment < 1:
        return []
    count = math.ceil(n_units / per_segment)
    out: list[tuple[int, int]] = []
    for i in range(count):
        start = i * per_segment
        end = min(n_units - 1, start + per_segment - 1)
        out.append((start, end))
    return out


def segment_capsule(geom: HelixGeometry, start: int, end: int) -> Capsule:
    """The kernel capsule covering unit range ``[start, end]`` — the
    duplex radius around the chord between the two units' origins.

    Chord, not arc: a segment is one lattice repeat of an origami helix,
    and the sagitta over 21 units of a centre line bent at the 10 nm
    default bend radius is under 0.15 nm. A design bending tighter than
    that is what ``chain_bend`` is for, and that check reads the path's own
    samples, not the capsules.
    """
    return Capsule(geom.origin(start), geom.origin(end), geom.motif.radius)


def segment_envelope(capsule: Capsule) -> str:
    """The cad mini-DSL envelope for a capsule — bare metres, since an se
    envelope is canonical/storage mode (``cad.dsl.parse``'s
    ``require_units=False`` default, and the same rule the atomic mode's
    hand-authored ``sphere:r2e-10`` follows).

    A single-unit segment's capsule has zero length, and a zero-height
    cylinder is not a solid — that case becomes the sphere the capsule
    actually is.
    """
    from precis.utils.units import format_dsl_number

    r = format_dsl_number(capsule.r)
    if capsule.length <= 0.0:
        return f"sphere:r{r}"
    return f"cyl:r{r}h{format_dsl_number(capsule.length)}"


def local_capsule(
    capsule: Capsule, parent_pose: list[float], parent_rot: list[float]
) -> Capsule:
    """``capsule`` expressed in the parent block's frame — the pose a child
    block stores (:attr:`precis_se.ops.SeBlock.local_pose` is
    parent-relative).

    Both endpoints are transformed and the capsule rebuilt, rather than
    composing the world pose with the parent's: that way exactly one Euler
    extraction happens (the kernel's, in
    :func:`precis_chain.envelope.capsule_pose`), so a rotated helix parent
    costs no Euler round-trip at all.
    """
    from precis.cad.vec import as_vec3, pose

    inverse = pose(as_vec3(parent_pose), as_vec3(parent_rot)).inverse()
    return Capsule(
        np.asarray(inverse.apply(as_vec3([float(v) for v in capsule.a])), dtype=float),
        np.asarray(inverse.apply(as_vec3([float(v) for v in capsule.b])), dtype=float),
        capsule.r,
    )


def segment_pose(capsule: Capsule) -> tuple[list[float], list[float]]:
    """``(pose, rot)`` for a segment's envelope, from the kernel's
    :func:`precis_chain.envelope.capsule_pose`.

    The kernel's origin is the capsule's ``a`` end (its documented
    deviation from the spec's midpoint), which is exactly where the cad
    ``cyl`` primitive's base belongs — so the pose goes through unchanged.
    """
    origin, euler, _length = capsule_pose(capsule)
    return ([float(v) for v in origin], [float(a) for a in euler])
