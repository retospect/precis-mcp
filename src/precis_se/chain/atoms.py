"""Atoms for a region of a helix — the Arnott B-DNA fibre templates placed
in the chain's own per-unit frames.

The pure geometry half of ``realize_chain``
(:func:`precis_se.atomic.generate.prepare_realize_chain` does every tree
read and calls :func:`build_region` here). Nothing in this module knows
about blocks, stores or domain rows: it takes placed units (an origin, a
frame and the occupants at each offset), optional loop curves, and returns
coordinates, elements, PDB naming, bonds and the port→atom map.

The templates
-------------
One nucleotide per base, heavy atoms only, from the fibre-diffraction
B-DNA model of Arnott and co-workers (Arnott & Hukins, *BBRC* 47:1504,
1972, doi:10.1016/0006-291X(72)90243-4; Chandrasekaran & Arnott,
Landolt-Börnstein VII/1b, 1989), transcribed from the Nucleic Acid Builder's
``fd_helix()`` ``"abdna"`` table (AmberClassic, ``src/nab/fd_helix.nab``,
GPL-2.0-or-later — a table of published model coordinates, cited rather
than vendored) and cross-checked against Open Babel's independent
transcription of the same model (``fastaformat.cpp``; the two agree to
0.14 Å rmsd over all 82 heavy atoms). The model is 10.0 bp/turn (36°,
3.38 Å); it is placed here at the *motif's* rise and twist
(:data:`precis_se.chain.nucleic.B_DNA_RISE_M`, 10.5 bp/turn or a lattice
retune), so the intra-nucleotide geometry is the model's and the
inter-nucleotide step is the design's. The O3'–P step across that
re-spacing is 1.53–1.60 Å against the model's 1.60 Å.

**Frame convention**: template ``z`` is the helix axis, ``x`` the base
pair's pseudo-dyad — the minor-groove bisector — and the template is one
nucleotide of the strand running 5'→3' toward ``+z`` with its phosphate at
azimuth −94.2° (−71.4° at the base-pair plane, against the space plan's
``−δ/2 = −72°``, :func:`precis_se.chain.nucleic.strand_azimuth_rad`). That
is exactly a chain unit frame's ``(n, b, t)`` triple, so a forward-strand
nucleotide at unit ``k`` is ``origin_k + x·n_k + y·b_k + z·t_k`` and the
reverse strand's is the same with ``(x, y, z) → (x, −y, −z)`` — the 180°
rotation about the dyad. The frames already carry the twist, so the
template needs no rotation of its own.

**Chirality** is not assumed: :func:`helix_handedness` measures it from
the placed phosphates, and the tests hold it right-handed.

What is deliberately NOT here: hydrogen atoms, A-RNA templates (an RNA
helix is refused upstream, never approximated with B-DNA), base-pair
H-bonds as bonds, and any per-step propeller/roll from a fitted model.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: The model's own helical parameters — what the template coordinates were
#: refined at. The placement uses the motif's, not these.
TEMPLATE_RISE_A = 3.38
TEMPLATE_TWIST_DEG = 36.0

#: The three atoms ``fidelity='backbone'`` keeps.
BACKBONE_ATOMS = ("P", "C4'", "C1'")

FIDELITIES = ("backbone", "allatom")

#: PDB residue names per letter; ``DN`` is an unsequenced nucleotide
#: (backbone atoms only — there is no base to place).
RESNAMES = {"A": "DA", "T": "DT", "G": "DG", "C": "DC", None: "DN"}

# Verbatim NAB "abdna" table: (atom, element, r [Å], phi [deg], z [Å]).
# As printed it is the strand-2 orientation (NAB's own chain-1 loop
# applies (phi, z) → (−phi, −z)); `_template` below does the same, so
# TEMPLATES holds the strand running 5'→3' toward +z.
_BACKBONE_CYL: tuple[tuple[str, str, float, float, float], ...] = (
    ("P", "P", 8.973, 94.20, 2.141),
    ("OP1", "O", 10.292, 90.28, 2.000),
    ("OP2", "O", 8.919, 102.25, 1.359),
    ("O5'", "O", 7.838, 86.88, 1.788),
    ("C5'", "C", 7.647, 79.12, 2.756),
    ("C4'", "C", 7.519, 69.00, 2.053),
    ("O4'", "O", 6.151, 66.25, 1.823),
    ("C3'", "C", 8.159, 68.80, 0.665),
    ("O3'", "O", 8.696, 60.18, 0.307),
    ("C2'", "C", 7.016, 72.17, -0.247),
    ("C1'", "C", 5.811, 66.60, 0.452),
)
_BASE_CYL: dict[str, tuple[tuple[str, str, float, float, float], ...]] = {
    "A": (
        ("N9", "N", 4.601, 76.19, 0.370),
        ("C8", "C", 4.836, 92.72, 0.385),
        ("N7", "N", 3.971, 105.33, 0.296),
        ("C5", "C", 2.730, 94.61, 0.217),
        ("C6", "C", 1.430, 109.71, 0.105),
        ("N6", "N", 1.910, 154.18, 0.051),
        ("N1", "N", 0.773, 41.89, 0.051),
        ("C2", "C", 2.077, 29.84, 0.105),
        ("N3", "N", 3.155, 46.59, 0.209),
        ("C4", "C", 3.288, 70.63, 0.262),
    ),
    "G": (
        ("N9", "N", 4.601, 76.19, 0.370),
        ("C8", "C", 4.833, 92.75, 0.384),
        ("N7", "N", 3.958, 105.52, 0.295),
        ("C5", "C", 2.714, 94.60, 0.216),
        ("C6", "C", 1.429, 111.85, 0.103),
        ("O6", "O", 1.816, 154.21, 0.047),
        ("N1", "N", 0.828, 40.69, 0.053),
        ("C2", "C", 2.176, 28.04, 0.106),
        ("N2", "N", 2.893, 2.14, 0.043),
        ("N3", "N", 3.205, 46.30, 0.212),
        ("C4", "C", 3.284, 70.42, 0.261),
    ),
    "T": (
        ("N1", "N", 4.601, 76.19, 0.370),
        ("C2", "C", 3.375, 67.28, 0.265),
        ("O2", "O", 3.557, 47.25, 0.237),
        ("N3", "N", 2.349, 86.11, 0.191),
        ("C4", "C", 3.029, 112.13, 0.214),
        ("O4", "O", 2.903, 135.96, 0.141),
        ("C5", "C", 4.422, 106.49, 0.327),
        ("C7", "C", 5.499, 118.64, 0.358),
        ("C6", "C", 5.015, 91.81, 0.400),
    ),
    "C": (
        ("N1", "N", 4.601, 76.19, 0.370),
        ("C2", "C", 3.351, 67.37, 0.263),
        ("O2", "O", 3.606, 47.32, 0.241),
        ("N3", "N", 2.296, 85.02, 0.187),
        ("C4", "C", 2.990, 110.26, 0.215),
        ("N4", "N", 2.859, 136.26, 0.138),
        ("C5", "C", 4.399, 106.56, 0.325),
        ("C6", "C", 5.007, 91.73, 0.399),
    ),
}

_BACKBONE_BONDS: tuple[tuple[str, str], ...] = (
    ("P", "OP1"),
    ("P", "OP2"),
    ("P", "O5'"),
    ("O5'", "C5'"),
    ("C5'", "C4'"),
    ("C4'", "O4'"),
    ("C4'", "C3'"),
    ("C3'", "O3'"),
    ("C3'", "C2'"),
    ("C2'", "C1'"),
    ("C1'", "O4'"),
)
_BASE_BONDS: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (
        ("C1'", "N9"),
        ("N9", "C8"),
        ("C8", "N7"),
        ("N7", "C5"),
        ("C5", "C6"),
        ("C6", "N6"),
        ("C6", "N1"),
        ("N1", "C2"),
        ("C2", "N3"),
        ("N3", "C4"),
        ("C4", "C5"),
        ("C4", "N9"),
    ),
    "G": (
        ("C1'", "N9"),
        ("N9", "C8"),
        ("C8", "N7"),
        ("N7", "C5"),
        ("C5", "C6"),
        ("C6", "O6"),
        ("C6", "N1"),
        ("N1", "C2"),
        ("C2", "N2"),
        ("C2", "N3"),
        ("N3", "C4"),
        ("C4", "C5"),
        ("C4", "N9"),
    ),
    "T": (
        ("C1'", "N1"),
        ("N1", "C2"),
        ("C2", "O2"),
        ("C2", "N3"),
        ("N3", "C4"),
        ("C4", "O4"),
        ("C4", "C5"),
        ("C5", "C7"),
        ("C5", "C6"),
        ("C6", "N1"),
    ),
    "C": (
        ("C1'", "N1"),
        ("N1", "C2"),
        ("C2", "O2"),
        ("C2", "N3"),
        ("N3", "C4"),
        ("C4", "N4"),
        ("C4", "C5"),
        ("C5", "C6"),
        ("C6", "N1"),
    ),
}
#: ``fidelity='backbone'``'s pseudo-bonds: the trace, not chemistry.
_BACKBONE_TRACE_BONDS: tuple[tuple[str, str], ...] = (("P", "C4'"), ("C4'", "C1'"))

#: Attachment-site atoms per base: ``(atom, axis_atom)`` — the atom a port
#: binds and the neighbour whose bond direction is the port's measured
#: axle (:func:`precis_se.atomic.bind.bind_structure`'s object form).
#: ``c5m`` is the major-groove C–H a 5-position modification replaces
#: (thymine's methyl carbon; C5 on cytosine; C8 on a purine, the analogous
#: edge). ``maj``/``min`` are the groove-facing heteroatoms.
SITE_ATOMS: dict[str, dict[str, tuple[str, str]]] = {
    "c5m": {"A": ("C8", "N7"), "G": ("C8", "N7"), "T": ("C7", "C5"), "C": ("C5", "C4")},
    "maj": {"A": ("N7", "C5"), "G": ("N7", "C5"), "T": ("O4", "C4"), "C": ("N4", "C4")},
    "min": {"A": ("N3", "C4"), "G": ("N3", "C4"), "T": ("O2", "C2"), "C": ("O2", "C2")},
}
SITE_KINDS = ("c5m", "maj", "min")


def _cart(
    rows: tuple[tuple[str, str, float, float, float], ...],
) -> list[tuple[str, str, np.ndarray]]:
    """NAB rows → strand-1 orientation (``phi → −phi``, ``z → −z``), Å."""
    out: list[tuple[str, str, np.ndarray]] = []
    for name, element, r, phi_deg, z in rows:
        phi = -math.radians(phi_deg)
        out.append(
            (name, element, np.array([r * math.cos(phi), r * math.sin(phi), -z]))
        )
    return out


#: ``letter → [(atom name, element, xyz Å)]`` in the strand-1 orientation;
#: ``None`` is the backbone alone.
TEMPLATES: dict[str | None, list[tuple[str, str, np.ndarray]]] = {
    None: _cart(_BACKBONE_CYL),
    **{letter: _cart(_BACKBONE_CYL + rows) for letter, rows in _BASE_CYL.items()},
}


def nucleotide_template(
    letter: str | None, *, fidelity: str, forward: bool
) -> list[tuple[str, str, np.ndarray]]:
    """The atoms of one nucleotide in the unit frame — mirrored through the
    dyad for the reverse strand, trimmed to :data:`BACKBONE_ATOMS` at
    ``fidelity='backbone'``."""
    if fidelity not in FIDELITIES:
        raise ValueError(f"fidelity must be one of {FIDELITIES}, got {fidelity!r}")
    if letter is not None and letter not in _BASE_CYL:
        raise ValueError(f"no template for letter {letter!r} (B-DNA: A/T/G/C)")
    rows = TEMPLATES[letter]
    if fidelity == "backbone":
        rows = [r for r in rows if r[0] in BACKBONE_ATOMS]
    if forward:
        return [(n, e, xyz.copy()) for n, e, xyz in rows]
    return [(n, e, xyz * np.array([1.0, -1.0, -1.0])) for n, e, xyz in rows]


def nucleotide_bonds(
    letter: str | None, *, fidelity: str
) -> tuple[tuple[str, str], ...]:
    """Intra-nucleotide bonds by atom name for the template above."""
    if fidelity == "backbone":
        return _BACKBONE_TRACE_BONDS
    if letter is None:
        return _BACKBONE_BONDS
    return _BACKBONE_BONDS + _BASE_BONDS[letter]


@dataclass(frozen=True)
class UnitOccupant:
    """One strand's nucleotide at one placed unit."""

    strand: str
    ord: int
    forward: bool
    letter: str | None
    #: The letters of the bases a register insertion adds after this one,
    #: 5'→3' (``None`` = unsequenced) — placed as a bulge toward the
    #: strand's next offset, not on the duplex.
    extra: tuple[str | None, ...] = ()


@dataclass(frozen=True)
class PlacedUnit:
    """A helix unit the region covers: where it is, how it is rolled, who
    sits on it. ``frame`` is the kernel's ``(t, n, b)``-column matrix,
    ``origin_A`` in ångström in the frame the atoms should come out in."""

    offset: int
    origin_A: np.ndarray
    frame: np.ndarray
    occupants: tuple[UnitOccupant, ...]


@dataclass(frozen=True)
class LoopNts:
    """A placed loop of one strand, between two nucleotides of this region:
    the one it leaves (``exit_offset``, same strand) and the one it
    arrives at (``entry_offset``). ``points_A`` is the sampled curve from
    exit to entry, ångström, same frame as the units. ``letters`` has one
    entry per loop nucleotide (``None`` = unsequenced); an empty tuple is
    a 0-nt crossover — one bond, no atoms."""

    strand: str
    exit_ord: int
    exit_offset: int
    entry_ord: int
    entry_offset: int
    letters: tuple[str | None, ...]
    points_A: np.ndarray


@dataclass(frozen=True)
class PortAtoms:
    """A port to add on the segment and bind: the atom, the axle partner
    and the phase atom (indices into :attr:`RegionAtoms.coords_A`), the
    element the port expects, and what to record on it."""

    atom: int
    axis_atom: int
    phase_atom: int
    expected_element: str
    annotations: dict[str, Any]


@dataclass
class RegionAtoms:
    """:func:`build_region`'s answer. Atom order is chain by chain, each
    chain 5'→3'; ``chains`` maps a PDB chain id to the strand it is."""

    coords_A: np.ndarray
    elements: list[str]
    names: list[str]
    resnames: list[str]
    resseq: list[int]
    chain_ids: list[str]
    bonds: list[tuple[int, int]]
    ports: dict[str, PortAtoms]
    chains: dict[str, str]
    notes: list[str] = field(default_factory=list)
    #: Per residue, in atom order: ``(chain id, resseq, strand, ord, offset
    #: or None for a loop nucleotide, letter, insertion index)`` — the
    #: index is 0 for the base on the unit and ``i`` for the ``i``-th base
    #: a register insertion adds after it (``stem@12+1``).
    residues: list[tuple[str, int, str, int, int | None, str | None, int]] = field(
        default_factory=list
    )

    @property
    def n_atoms(self) -> int:
        return int(self.coords_A.shape[0])

    @property
    def n_residues(self) -> int:
        return len(self.residues)


@dataclass
class _Residue:
    strand: str
    ord: int
    offset: int | None
    letter: str | None
    forward: bool
    atoms: list[tuple[str, str, np.ndarray]]
    bonds: tuple[tuple[str, str], ...]
    loop_key: tuple[int, int] | None = None  # (exit_ord, entry_ord) when on a loop
    ins: int = 0  # i-th inserted base after the one on the unit; 0 = on the unit


def _place(unit: PlacedUnit, xyz: np.ndarray) -> np.ndarray:
    """Template ``(x, y, z)`` → ``origin + x·n + y·b + z·t``."""
    f = unit.frame
    return unit.origin_A + xyz[0] * f[:, 1] + xyz[1] * f[:, 2] + xyz[2] * f[:, 0]


def _resample(points: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """``n`` points at equal arc-length fractions ``(i+1)/(n+1)`` along the
    polyline, with unit tangents there."""
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[0] < 2:
        raise ValueError("a loop curve needs at least two points")
    seg = np.diff(pts, axis=0)
    seg_len = np.linalg.norm(seg, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    total = float(cum[-1])
    if total <= 0.0:
        raise ValueError("a loop curve has zero length")
    out_p = np.zeros((n, 3))
    out_t = np.zeros((n, 3))
    for i in range(n):
        s = total * (i + 1) / (n + 1)
        j = int(np.searchsorted(cum, s, side="right") - 1)
        j = min(max(j, 0), seg_len.shape[0] - 1)
        frac = 0.0 if seg_len[j] <= 0 else (s - cum[j]) / seg_len[j]
        out_p[i] = pts[j] + frac * seg[j]
        direction = seg[j] / seg_len[j] if seg_len[j] > 0 else np.array([0.0, 0.0, 1.0])
        out_t[i] = direction
    return out_p, out_t


def _loop_frames(
    points: np.ndarray, tangents: np.ndarray, seed: np.ndarray
) -> list[np.ndarray]:
    """A ``(t, n, b)``-column frame per loop point: ``t`` the curve
    tangent, ``n`` the running perpendicular seeded from ``seed`` (the
    exiting nucleotide's own normal), carried by projection."""
    frames: list[np.ndarray] = []
    n_prev = np.asarray(seed, dtype=float)
    for t in tangents:
        t = t / (np.linalg.norm(t) or 1.0)
        n = n_prev - np.dot(n_prev, t) * t
        norm = float(np.linalg.norm(n))
        if norm < 1e-9:
            least = int(np.argmin(np.abs(t)))
            axis = np.zeros(3)
            axis[least] = 1.0
            n = axis - np.dot(axis, t) * t
            norm = float(np.linalg.norm(n))
        n = n / norm
        b = np.cross(t, n)
        frames.append(np.column_stack([t, n, b]))
        n_prev = n
    return frames


def _route_key(res: _Residue) -> tuple[int, int, int, int]:
    """5'→3' order within one strand: by ord, then along the domain
    (ascending offsets forward, descending reverse), an offset's inserted
    bases right after its own; loop nucleotides sort between their exit
    and entry ords."""
    if res.loop_key is not None:
        return (res.loop_key[0], 1, res.offset if res.offset is not None else 0, 0)
    pos = res.offset if res.offset is not None else 0
    return (res.ord, 0, pos if res.forward else -pos, res.ins)


#: How far an inserted base's template is pushed out from the duplex
#: position it would hold (Å, radially, past its own phosphate) — a start
#: point the loop relax pulls back onto the backbone, not a conformation.
INSERT_BULGE_A = 8.0


def _bulge_residues(
    unit: PlacedUnit,
    occ: UnitOccupant,
    on_unit: list[tuple[str, str, np.ndarray]],
    by_offset: dict[int, PlacedUnit],
    *,
    fidelity: str,
) -> list[_Residue]:
    """The ``occ.extra`` inserted bases after ``occ``'s base on ``unit``:
    each a template in the unit's own frame, its origin stepped toward the
    strand's next offset (``i/(k+1)`` of the way) and pushed radially out
    past the phosphate by :data:`INSERT_BULGE_A`. Geometry only — the
    realizer's loop relax chains them; nothing here predicts a bulge."""
    step = 1 if occ.forward else -1
    nxt = by_offset.get(unit.offset + step)
    prv = by_offset.get(unit.offset - step)
    if nxt is not None:
        toward = nxt.origin_A - unit.origin_A
    elif prv is not None:
        toward = unit.origin_A - prv.origin_A
    else:
        toward = step * TEMPLATE_RISE_A * unit.frame[:, 0]
    axis = unit.frame[:, 0]
    p_atom = next(xyz for name, _e, xyz in on_unit if name in ("P", "C4'"))
    radial = p_atom - unit.origin_A
    radial = radial - np.dot(radial, axis) * axis
    radial = radial / (np.linalg.norm(radial) or 1.0)
    k = len(occ.extra)
    out: list[_Residue] = []
    for i, letter in enumerate(occ.extra, start=1):
        shift = toward * i / (k + 1) + INSERT_BULGE_A * radial
        moved = PlacedUnit(
            offset=unit.offset,
            origin_A=unit.origin_A + shift,
            frame=unit.frame,
            occupants=(),
        )
        template = nucleotide_template(letter, fidelity=fidelity, forward=occ.forward)
        out.append(
            _Residue(
                strand=occ.strand,
                ord=occ.ord,
                offset=unit.offset,
                letter=letter,
                forward=occ.forward,
                atoms=[(n, e, _place(moved, xyz)) for n, e, xyz in template],
                bonds=nucleotide_bonds(letter, fidelity=fidelity),
                ins=i,
            )
        )
    return out


def build_region(
    units: list[PlacedUnit],
    *,
    fidelity: str = "allatom",
    sites: tuple[int, ...] = (),
    loops: tuple[LoopNts, ...] = (),
    deletions: frozenset[int] = frozenset(),
) -> RegionAtoms:
    """Atoms for every nucleotide on ``units`` (plus the placed ``loops``),
    bonded along each strand, with the port→atom map.

    Register insertions and deletions: a ``deletions`` offset holds no
    unit, and the strand's residues either side of it are bonded across
    the gap — on frames kept at the lattice twist that step is stretched by
    a rise per deleted offset, and ``notes`` says how long it came out. An
    occupant's ``extra`` letters are placed as a bulge after its own base
    (:func:`_bulge_residues`), bonded in route order.

    Chain ids are minted per strand in order of first appearance along
    the region (``A``, ``B``, …); a strand that leaves the region through
    a placed loop and comes back is one chain. Consecutive residues of a
    strand get an O3'–P bond when they are adjacent on the helix (same
    domain, neighbouring offsets), or when a ``LoopNts`` joins them; two
    consecutive domains with no placed loop stay unbonded and the gap is
    named in ``notes``.

    ``sites`` are unit offsets that get ``n<offset>_c5m``/``_maj``/``_min``
    ports on the forward occupant's base (the reverse one when only that
    is present) — ``fidelity='allatom'`` only, and the nucleotide must be
    sequenced: both are refused, never approximated. (Underscore, not the
    spec's dot: a port name may not contain ``.``, the block.port endpoint
    separator.)
    """
    if fidelity not in FIDELITIES:
        raise ValueError(f"fidelity must be one of {FIDELITIES}, got {fidelity!r}")
    units = sorted(units, key=lambda u: u.offset)
    by_offset = {u.offset: u for u in units}
    notes: list[str] = []

    # 1. one residue per occupant per unit
    residues: list[_Residue] = []
    for unit in units:
        for occ in unit.occupants:
            template = nucleotide_template(
                occ.letter, fidelity=fidelity, forward=occ.forward
            )
            residues.append(
                _Residue(
                    strand=occ.strand,
                    ord=occ.ord,
                    offset=unit.offset,
                    letter=occ.letter,
                    forward=occ.forward,
                    atoms=[(n, e, _place(unit, xyz)) for n, e, xyz in template],
                    bonds=nucleotide_bonds(occ.letter, fidelity=fidelity),
                )
            )
            if occ.extra:
                residues += _bulge_residues(
                    unit, occ, residues[-1].atoms, by_offset, fidelity=fidelity
                )
    if not residues:
        raise ValueError(
            "no strand occupies any unit of the region — nothing to realize"
        )

    # 2. loop nucleotides along their curves
    loop_index: dict[tuple[str, int, int], LoopNts] = {}
    for loop in loops:
        key = (loop.strand, loop.exit_ord, loop.entry_ord)
        loop_index[key] = loop
        if not loop.letters:
            continue
        exit_unit = by_offset.get(loop.exit_offset)
        seed = (
            exit_unit.frame[:, 1]
            if exit_unit is not None
            else np.array([1.0, 0.0, 0.0])
        )
        points, tangents = _resample(loop.points_A, len(loop.letters))
        frames = _loop_frames(points, tangents, seed)
        p_template = next(xyz for n, _e, xyz in TEMPLATES[None] if n == "P")
        for i, (letter, point, frame) in enumerate(
            zip(loop.letters, points, frames, strict=True)
        ):
            template = nucleotide_template(letter, fidelity=fidelity, forward=True)
            unit = PlacedUnit(offset=-1, origin_A=point, frame=frame, occupants=())
            residues.append(
                _Residue(
                    strand=loop.strand,
                    ord=loop.exit_ord,
                    offset=None,
                    letter=letter,
                    forward=True,
                    atoms=[
                        (n, e, _place(unit, xyz - p_template)) for n, e, xyz in template
                    ],
                    bonds=nucleotide_bonds(letter, fidelity=fidelity),
                    loop_key=(loop.exit_ord, loop.entry_ord),
                )
            )
            # a loop nucleotide's "offset" is its index along the loop,
            # only for ordering (loop_key sorts it between the domains)
            residues[-1].offset = i

    # 3. chains: per strand, 5'→3'
    strands_in_order: list[str] = []
    for res in residues:
        if res.strand not in strands_in_order:
            strands_in_order.append(res.strand)
    chains: dict[str, str] = {}
    chain_of: dict[str, str] = {}
    for i, strand in enumerate(strands_in_order):
        cid = chr(ord("A") + i) if i < 26 else str(i)
        chains[cid] = strand
        chain_of[strand] = cid

    coords: list[np.ndarray] = []
    elements: list[str] = []
    names: list[str] = []
    resnames: list[str] = []
    resseq: list[int] = []
    chain_ids: list[str] = []
    bonds: list[tuple[int, int]] = []
    residue_rows: list[tuple[str, int, str, int, int | None, str | None, int]] = []
    #: (strand, ord, offset, loop entry ord, insertion index) → {atom name:
    #: index}, for sites + ports
    atom_index: dict[_AtomKey, dict[str, int]] = {}
    chain_ends: dict[str, tuple[_Residue, _Residue]] = {}

    for strand in strands_in_order:
        route = sorted((r for r in residues if r.strand == strand), key=_route_key)
        cid = chain_of[strand]
        prev: _Residue | None = None
        prev_atoms: dict[str, int] = {}
        for seq_no, res in enumerate(route, start=1):
            base = len(coords)
            local: dict[str, int] = {}
            for name, element, xyz in res.atoms:
                local[name] = base + len(local)
                coords.append(xyz)
                elements.append(element)
                names.append(name)
                resnames.append(RESNAMES[res.letter])
                resseq.append(seq_no)
                chain_ids.append(cid)
            for a, b in res.bonds:
                if a in local and b in local:
                    bonds.append((local[a], local[b]))
            residue_rows.append(
                (
                    cid,
                    seq_no,
                    strand,
                    res.ord,
                    None if res.loop_key else res.offset,
                    res.letter,
                    res.ins,
                )
            )
            atom_index[_atom_key(res)] = local
            if prev is not None:
                if _adjacent(prev, res, loop_index, deletions):
                    tail = "O3'" if fidelity == "allatom" else "C4'"
                    if tail in prev_atoms and "P" in local:
                        bonds.append((prev_atoms[tail], local["P"]))
                        gap = _deleted_between(prev, res, deletions)
                        if gap:
                            step = float(
                                np.linalg.norm(
                                    coords[local["P"]] - coords[prev_atoms[tail]]
                                )
                            )
                            notes.append(
                                f"{strand}: {tail}–P bonded across deleted "
                                f"offset(s) {gap} at {step:.2f} Å — the duplex "
                                "keeps its lattice positions, so this step is "
                                "stretched by the missing rise (strained, not "
                                "relaxed)"
                            )
                else:
                    notes.append(
                        f"{strand}: no bond between domain {prev.ord} and "
                        f"{res.ord} — the loop between them is not placed "
                        "in this region (relax_chain places loops; a loop "
                        "whose other end is outside the region stays open)"
                    )
            prev, prev_atoms = res, local
        chain_ends[strand] = (route[0], route[-1])

    coords_A = np.asarray(coords, dtype=float).reshape(-1, 3)

    # 4. ports
    ports: dict[str, PortAtoms] = {}
    n_fwd = n_rev = 0
    for strand in strands_in_order:
        first, last = chain_ends[strand]
        forward = first.forward
        if forward:
            n_fwd += 1
            suffix = "" if n_fwd == 1 else str(n_fwd)
            five, three = f"5p{suffix}", f"3p{suffix}"
        else:
            n_rev += 1
            suffix = "" if n_rev == 1 else str(n_rev)
            five, three = f"r5p{suffix}", f"r3p{suffix}"
        first_atoms = _atoms_of(atom_index, first)
        last_atoms = _atoms_of(atom_index, last)
        if fidelity == "allatom":
            five_spec = ("P", "O5'", "C1'", "P")
            three_spec = ("O3'", "C3'", "C1'", "O")
        else:
            five_spec = ("P", "C4'", "C1'", "P")
            three_spec = ("C4'", "P", "C1'", "C")
        for pname, spec, res, atoms, end in (
            (five, five_spec, first, first_atoms, "5'"),
            (three, three_spec, last, last_atoms, "3'"),
        ):
            atom, axis, phase, element = spec
            ports[pname] = PortAtoms(
                atom=atoms[atom],
                axis_atom=atoms[axis],
                phase_atom=atoms[phase],
                expected_element=element,
                annotations={
                    "strand": strand,
                    "end": end,
                    "offset": res.offset if res.loop_key is None else None,
                    "ord": res.ord,
                },
            )

    for offset in sites:
        site_unit = by_offset.get(offset)
        if site_unit is None:
            raise ValueError(f"site {offset} is not a unit of this region")
        if fidelity != "allatom":
            raise ValueError(
                f"site {offset}: attachment ports need fidelity='allatom' — "
                "the backbone trace has no base atoms to attach to"
            )
        occupant = next((o for o in site_unit.occupants if o.forward), None) or next(
            iter(site_unit.occupants), None
        )
        if occupant is None:
            raise ValueError(f"site {offset}: no strand occupies that unit")
        if occupant.letter is None:
            raise ValueError(
                f"site {offset}: strand {occupant.strand!r} carries no sequence "
                "there — an attachment site needs a base to attach to"
            )
        res_atoms = atom_index[(occupant.strand, occupant.ord, offset, None, 0)]
        for kind in SITE_KINDS:
            atom, axis = SITE_ATOMS[kind][occupant.letter]
            ports[f"n{offset}_{kind}"] = PortAtoms(
                atom=res_atoms[atom],
                axis_atom=res_atoms[axis],
                phase_atom=res_atoms["C1'"],
                expected_element=elements[res_atoms[atom]],
                annotations={
                    "strand": occupant.strand,
                    "offset": offset,
                    "site": kind,
                    "base": occupant.letter,
                },
            )

    return RegionAtoms(
        coords_A=coords_A,
        elements=elements,
        names=names,
        resnames=resnames,
        resseq=resseq,
        chain_ids=chain_ids,
        bonds=bonds,
        ports=ports,
        chains=chains,
        notes=notes,
        residues=residue_rows,
    )


_AtomKey = tuple[str, int, int | None, int | None, int]


def _atom_key(res: _Residue) -> _AtomKey:
    return (
        res.strand,
        res.ord,
        res.offset,
        res.loop_key[1] if res.loop_key else None,
        res.ins,
    )


def _atoms_of(index: dict[_AtomKey, dict[str, int]], res: _Residue) -> dict[str, int]:
    return index[_atom_key(res)]


def _deleted_between(
    prev: _Residue, res: _Residue, deletions: frozenset[int]
) -> list[int]:
    """The deleted offsets strictly between two domain residues of one
    domain — the gap a bond across a deletion spans."""
    if prev.offset is None or res.offset is None:
        return []
    lo, hi = sorted((prev.offset, res.offset))
    return [o for o in range(lo + 1, hi) if o in deletions]


def _adjacent(
    prev: _Residue,
    res: _Residue,
    loops: dict[tuple[str, int, int], LoopNts],
    deletions: frozenset[int] = frozenset(),
) -> bool:
    """Whether ``prev`` → ``res`` is one backbone bond: neighbouring
    offsets of one domain (or two with only deleted offsets between them),
    an offset's next inserted base, consecutive loop nucleotides, a domain
    end into its placed loop (or out of it), or a placed 0-nt crossover."""
    if prev.loop_key is None and res.loop_key is None:
        if prev.ord == res.ord and prev.offset is not None and res.offset is not None:
            if prev.offset == res.offset:
                return res.ins == prev.ins + 1
            span = abs(prev.offset - res.offset)
            return res.ins == 0 and (
                span == 1 or len(_deleted_between(prev, res, deletions)) == span - 1
            )
        loop = loops.get((prev.strand, prev.ord, res.ord))
        return loop is not None and not loop.letters
    if prev.loop_key is not None and res.loop_key is not None:
        return (
            prev.loop_key == res.loop_key
            and (res.offset or 0) == (prev.offset or 0) + 1
        )
    if prev.loop_key is None and res.loop_key is not None:
        return res.loop_key[0] == prev.ord and (res.offset or 0) == 0
    # prev on a loop, res back on a domain
    assert prev.loop_key is not None
    loop = loops.get((prev.strand, prev.loop_key[0], prev.loop_key[1]))
    return (
        loop is not None
        and res.ord == prev.loop_key[1]
        and (prev.offset or 0) == len(loop.letters) - 1
    )


def helix_handedness(region: RegionAtoms, *, chain_id: str = "A") -> float:
    """The signed twist per residue of chain ``chain_id``'s phosphates
    about the chain's own axis, radians — positive is right-handed. The
    axis is the principal direction of the P cloud, so this reads the
    placed atoms alone and assumes nothing about the frames they came
    from."""
    p = np.array(
        [
            region.coords_A[i]
            for i, (name, cid) in enumerate(
                zip(region.names, region.chain_ids, strict=True)
            )
            if name == "P" and cid == chain_id
        ]
    )
    if p.shape[0] < 3:
        raise ValueError("handedness needs at least three phosphates on the chain")
    centre = p.mean(axis=0)
    _u, _s, vt = np.linalg.svd(p - centre)
    axis = vt[0]
    if np.dot(p[-1] - p[0], axis) < 0:
        axis = -axis
    radial = (p - centre) - np.outer((p - centre) @ axis, axis)
    total = 0.0
    for a, b in itertools.pairwise(radial):
        total += math.atan2(float(np.dot(np.cross(a, b), axis)), float(np.dot(a, b)))
    return total / (p.shape[0] - 1)
