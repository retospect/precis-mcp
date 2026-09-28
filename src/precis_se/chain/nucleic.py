"""The nucleic-acid numbers — the one place in the repo that states them.

The :mod:`precis_chain` kernel is deliberately chemistry-free (its
:class:`~precis_chain.motif.Motif` docstring: "there is deliberately no
``B_DNA`` constant … the binding owns every number and cites its source").
This module is that binding's number table, and every constant below
carries its source in the comment above it.

**Sources.**

- **B-DNA** — rise 0.334 nm per base pair, 10.5 bp per turn, backbone
  radius 1.0 nm (duplex diameter 2.0 nm). Fibre-diffraction/crystal
  consensus for the B form; the 10.5 bp/turn figure is the *solution*
  value origami design uses (Watson–Crick's 10 bp/turn is the crystal
  fibre value). :data:`B_DNA_TWIST_RAD` is ``2 pi / 10.5`` **exactly**
  (34.2857°), not the 34.3° that number rounds to — see that constant's
  own note, the rounding breaks the honeycomb repeat.
- **A-RNA** — rise 0.28 nm, 11 bp per turn (32.727°), radius 1.15 nm
  (diameter 2.3 nm). A-form duplex consensus.
- **ssDNA contour** 0.63 nm per nucleotide — the fully-extended backbone
  bond length per base, the number every loop-reach calculation in origami
  design uses (Douglas et al. 2009, caDNAno; scadnano's loop rule).
- **Lattices** — honeycomb: crossovers every 7 bp, structural repeat
  21 bp = 2 turns (Douglas et al., *Nature* 459:414, 2009, the honeycomb
  origami paper); square: crossovers every 8 bp, repeat 32 bp = 3 turns
  (Ke et al., *JACS* 131:15903, 2009). The repeat fixes the lattice's
  *design* twist — 21 bp/2 turns is 10.5 bp/turn, 32 bp/3 turns is
  10.67 bp/turn — which is why :func:`lattice_motif` retunes the motif.
- **Inter-helix centre spacing** 2.5 nm for both lattices (scadnano's
  ``helix spacing`` default; measured 2.4–2.6 nm in Douglas 2009 /
  Ke 2009). One constant with a per-design ``min_gap`` override, per the
  2026-09-28 decision in ``docs/backlog/se-nucleic-acid.md``.
- **oxDNA length unit** 0.8518 nm (oxDNA's simulation unit of length) —
  carried here for the export item (``se-nucleic-realize-export``) so the
  number is transcribed once.
- **Persistence lengths** — dsDNA 50 nm, dsRNA 60 nm, ssDNA 2 nm
  (Hagerman 1988 / Abels 2005 / Tinland 1997 order of magnitude). These
  are *defaults*: the real values are salt- and sequence-dependent, so a
  design that cares states a ``material`` row with conditions and the
  handler-side findings read that instead. **The pure seams here use the
  coded default only** and say so.
- **Leontis–Westhof families** — the 12 base-pair families: 6 unordered
  pairs of the three edges ``{W, H, S}`` (Watson–Crick, Hoogsteen, Sugar)
  times ``{cis, trans}`` glycosidic-bond orientation (Leontis & Westhof,
  *NAR* 29:3497, 2001). :data:`ALLOWED_PAIRS` is a **curated** occupancy
  table per family, not an exhaustive one — see its own note.

Everything here is metres and radians, the se storage units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from precis_chain.motif import Motif
from precis_chain.register import Lattice

# ── motifs ──────────────────────────────────────────────────────────────

#: B-DNA rise per base pair, metres.
B_DNA_RISE_M = 0.334e-9
#: B-DNA base pairs per turn (solution value; the origami design number).
B_DNA_BP_PER_TURN = 10.5
#: B-DNA twist per base pair, radians — ``2 pi / 10.5`` exactly.
#:
#: This is the 34.3° the literature quotes, at full precision, and the
#: precision is load-bearing: at 34.3° a 21-bp honeycomb repeat misses two
#: whole turns by 21 * 0.0143° = 5.2e-3 rad, which is five times
#: :data:`precis_chain.register.REGISTER_TOL`, so every honeycomb design
#: in the world would report ``chain_twist_register``. Deriving the twist
#: from the bp-per-turn figure instead makes the repeat close exactly,
#: which is the whole reason the lattice picked 10.5.
B_DNA_TWIST_RAD = 2.0 * math.pi / B_DNA_BP_PER_TURN
#: B-DNA backbone radius (duplex radius), metres.
B_DNA_RADIUS_M = 1.0e-9

#: A-RNA rise per base pair, metres.
A_RNA_RISE_M = 0.28e-9
#: A-RNA base pairs per turn.
A_RNA_BP_PER_TURN = 11.0
#: A-RNA twist per base pair, radians (32.727°, from 11 bp/turn).
A_RNA_TWIST_RAD = 2.0 * math.pi / A_RNA_BP_PER_TURN
#: A-RNA backbone radius, metres.
A_RNA_RADIUS_M = 1.15e-9

#: Single-stranded backbone contour per nucleotide, metres — the bond
#: length loop reach is counted in (:mod:`precis_chain.loop`).
SS_CONTOUR_PER_NT_M = 0.63e-9

#: Persistence lengths, metres — coded DEFAULTS (module docstring).
LP_DSDNA_M = 50.0e-9
LP_DSRNA_M = 60.0e-9
LP_SSDNA_M = 2.0e-9

#: Default minimum centre-line bend radius, metres: Lp/5. A dsDNA duplex
#: bent tighter than a fifth of its persistence length is paying real
#: elastic energy. Reported at **warn** when it is this default and at
#: **error** when the design authored its own ``min_bend_radius``
#: (docs/backlog/se-nucleic-acid.md Constants).
DEFAULT_MIN_BEND_RADIUS_M = LP_DSDNA_M / 5.0

#: Inter-helix centre-to-centre spacing, metres — one constant for both
#: lattices, with a per-design ``min_gap`` override.
HELIX_SPACING_M = 2.5e-9
#: The LOW end of the measured spacing range (2.4–2.6 nm, Douglas 2009 /
#: Ke 2009), metres — what :func:`default_min_gap_m` measures the default
#: clash tolerance from rather than :data:`HELIX_SPACING_M` itself. Two
#: reasons, and the second is the one that bites: a real lattice's spacing
#: genuinely varies over that range, so flagging 2.45 nm would be flagging
#: a measurement; and a tolerance set exactly AT the nominal spacing is a
#: knife-edge float comparison — a site position built as ``row * 2.5e-9``
#: lands a few ULP under the gap it is compared against, so every helix
#: pair of a perfectly correct origami reported ``chain_clash`` (37 of them
#: in the rectangle fixture) until this constant existed.
HELIX_SPACING_MIN_M = 2.4e-9

#: oxDNA's unit of length, metres (transcribed once, for the export item).
OXDNA_UNIT_M = 0.8518e-9

#: The motifs by name, in se's metres/radians. ``min_bend_radius`` is the
#: Lp/5 default; ``contour_per_unit`` is the SINGLE-strand contour, which
#: is what the kernel's loop machinery wants from a duplex motif (see
#: :attr:`precis_chain.motif.Motif.contour_per_unit`).
MOTIFS: dict[str, Motif] = {
    "B-DNA": Motif(
        name="B-DNA",
        rise=B_DNA_RISE_M,
        twist=B_DNA_TWIST_RAD,
        radius=B_DNA_RADIUS_M,
        min_bend_radius=DEFAULT_MIN_BEND_RADIUS_M,
        persistence_length=LP_DSDNA_M,
        contour_per_unit=SS_CONTOUR_PER_NT_M,
    ),
    "A-RNA": Motif(
        name="A-RNA",
        rise=A_RNA_RISE_M,
        twist=A_RNA_TWIST_RAD,
        radius=A_RNA_RADIUS_M,
        min_bend_radius=LP_DSRNA_M / 5.0,
        persistence_length=LP_DSRNA_M,
        contour_per_unit=SS_CONTOUR_PER_NT_M,
    ),
}

#: Which nucleic acid each motif is made of — the default motif for a
#: strand/helix that names only ``nucleic``.
MOTIF_FOR_NUCLEIC = {"DNA": "B-DNA", "RNA": "A-RNA"}

#: The two nucleic acids this cut models.
NUCLEICS = ("DNA", "RNA")

# ── lattices ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LatticeSpec:
    """One origami lattice: its structural repeat, its crossover period,
    and the in-plane geometry of its neighbour sites.

    ``pitch_units``/``turns_per_pitch`` are the repeat
    (:func:`precis_chain.register.commensurate` takes the first and
    :func:`lattice_motif` derives the design twist from both);
    ``crossover_period`` is how often a crossover site recurs along one
    helix; ``neighbours`` are the in-plane unit directions from a site to
    its neighbours, at :data:`HELIX_SPACING_M`.

    The neighbour directions are given for an **even-parity** site
    (``(row + col) % 2 == 0``); an odd-parity honeycomb site's are the
    negatives (:func:`neighbour_directions`). A square site's are
    parity-independent.
    """

    name: str
    pitch_units: int
    turns_per_pitch: int
    crossover_period: int
    neighbours: tuple[tuple[float, float], ...]

    def kernel(self) -> Lattice:
        """The kernel's :class:`~precis_chain.register.Lattice` for this
        spec — neighbour **azimuths** rather than directions.

        The azimuth convention is the kernel's: measured in the unit frame
        from its normal. For the only case that declares a lattice — a
        straight helix along +z seeded with ``r0 = +x``
        (:func:`precis_se.chain.layout.helix_geometry`) — the unit frame's
        normal IS world +x, so the in-plane angle and the unit-frame
        azimuth are the same number.
        """
        return Lattice(
            name=self.name,
            neighbour_azimuths=tuple(math.atan2(y, x) for x, y in self.neighbours),
            period_units=self.pitch_units,
        )


_SQRT3_2 = math.sqrt(3.0) / 2.0

#: The lattices, by name. Honeycomb neighbours: two along ±x-ish at 30°
#: and 150°, one straight down — the three directions the house honeycomb
#: mapping in :func:`site_position` produces at unit spacing. Square: the
#: four axis directions.
LATTICES: dict[str, LatticeSpec] = {
    "honeycomb": LatticeSpec(
        name="honeycomb",
        pitch_units=21,
        turns_per_pitch=2,
        crossover_period=7,
        neighbours=((_SQRT3_2, 0.5), (-_SQRT3_2, 0.5), (0.0, -1.0)),
    ),
    "square": LatticeSpec(
        name="square",
        pitch_units=32,
        turns_per_pitch=3,
        crossover_period=8,
        neighbours=((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)),
    ),
}


def site_position(kind: str, row: int, col: int) -> tuple[float, float]:
    """In-plane ``(x, y)`` metres of lattice site ``(row, col)``.

    Square: ``(col, row) * spacing`` — the obvious mapping.

    Honeycomb, the house mapping (cadnano/scadnano's honeycomb geometry,
    re-derived here rather than transcribed so the property that matters
    is checkable): ``x = col * spacing * sqrt(3)/2``, ``y = spacing *
    (1.5 * row + 0.5 * ((row + col) % 2))``. The property it is built for
    is that every nearest neighbour sits **exactly**
    :data:`HELIX_SPACING_M` away and each site has three of them — a
    column step is ``(0.866, ±0.5) * spacing`` (parity flips) and the row
    step toward the parity partner is ``1.0 * spacing``.
    """
    spec = LATTICES.get(kind)
    if spec is None:
        raise KeyError(
            f"unknown lattice {kind!r}; known: {', '.join(sorted(LATTICES))}"
        )
    if kind == "square":
        return (col * HELIX_SPACING_M, row * HELIX_SPACING_M)
    parity = (row + col) % 2
    return (
        col * HELIX_SPACING_M * _SQRT3_2,
        HELIX_SPACING_M * (1.5 * row + 0.5 * parity),
    )


def neighbour_directions(
    kind: str, row: int, col: int
) -> tuple[tuple[float, float], ...]:
    """The in-plane unit directions from site ``(row, col)`` to its
    neighbours — :attr:`LatticeSpec.neighbours` for an even-parity site,
    negated for an odd-parity honeycomb one (the honeycomb's two
    sublattices point opposite ways; the square lattice has one)."""
    spec = LATTICES[kind]
    if kind == "square" or (row + col) % 2 == 0:
        return spec.neighbours
    return tuple((-x, -y) for x, y in spec.neighbours)


def lattice_motif(motif: Motif, lattice: str | None) -> Motif:
    """``motif`` with its twist retuned to ``lattice``'s design repeat.

    An origami lattice does not take the solution twist as given: it picks
    a repeat (21 bp / 2 turns, 32 bp / 3 turns) and the design's helices
    are *held* at that twist by the crossovers. So the register check has
    to be run against the lattice's own number, not the motif's:
    honeycomb's 10.5 bp/turn is a no-op for B-DNA (that is where B-DNA's
    10.5 came from), and square's 10.67 bp/turn is a real +1.6 % over-wind
    — the documented design twist of square-lattice origami, and the
    reason a square-lattice design is globally twisted unless it inserts
    or deletes bases.

    ``lattice=None`` returns ``motif`` unchanged (a free-path helix has no
    lattice holding its twist).
    """
    if lattice is None:
        return motif
    spec = LATTICES[lattice]
    twist = 2.0 * math.pi * spec.turns_per_pitch / spec.pitch_units
    sign = -1.0 if motif.twist < 0.0 else 1.0
    if abs(twist - abs(motif.twist)) < 1e-15:
        return motif
    return Motif(
        name=f"{motif.name}/{spec.name}",
        rise=motif.rise,
        twist=sign * twist,
        radius=motif.radius,
        min_bend_radius=motif.min_bend_radius,
        persistence_length=motif.persistence_length,
        contour_per_unit=motif.contour_per_unit,
    )


def default_min_gap_m(motif: Motif) -> float:
    """The default clash tolerance for ``motif``, metres: the surface gap
    two helices have when their axes sit at :data:`HELIX_SPACING_M`.

    A *gap* threshold, not a centre distance, because that is what
    :func:`precis_chain.clash.clashes` compares against — and the kernel's
    comparison is strict, so a design sitting exactly on the threshold
    passes (:mod:`precis_chain`'s "Clash and bend comparisons are strict"
    note). Measured from :data:`HELIX_SPACING_MIN_M`, not the nominal
    spacing, for the two reasons that constant states. For B-DNA that is
    ``2.4 − 2 × 1.0 = 0.4 nm``, so a pair at the nominal 2.5 nm clears it
    by 0.1 nm and a pair at 2.0 nm (zero surface gap) does not; for A-RNA's
    fatter duplex, 0.1 nm.
    """
    return HELIX_SPACING_MIN_M - 2.0 * motif.radius


# ── strand azimuths ─────────────────────────────────────────────────────

#: Where a duplex's two strands sit in the unit frame, radians: the
#: forward strand's backbone along the frame's normal (azimuth 0), the
#: reverse strand's on the far side (pi).
#:
#: The real B-DNA backbones are ~120°/240° apart across the minor groove,
#: not 180°. The antipodal simplification is deliberate and its one
#: consequence is stated here rather than buried: what every consumer
#: actually uses is the *difference* between two exits, and the design
#: layer's crossover rule is "the backbone faces the neighbour", which the
#: lattice's own crossover period already encodes. Modelling the groove
#: angle would change which of the two strands a crossover leaves from by
#: ~1/6 of a turn and change no reach number by more than the 0.35 nm the
#: two azimuths differ by — real, but smaller than the
#: :data:`SS_CONTOUR_PER_NT_M` bond it is compared against, and the fix is
#: a per-nucleic groove angle, which is an atoms-tier number
#: (``se-nucleic-realize-export``), not a space-plan one.
STRAND_AZIMUTH_RAD = {True: 0.0, False: math.pi}

# ── Leontis–Westhof pair families ───────────────────────────────────────

#: The three base edges.
EDGES = ("W", "H", "S")
#: The two glycosidic-bond orientations.
ORIENTATIONS = ("cis", "trans")

#: The 12 families, canonically spelled ``"<edge>-<edge>-<orientation>"``
#: with the edge pair UNORDERED (so ``H-W-cis`` canonicalises to
#: ``W-H-cis`` — :func:`canonical_geometry`).
FAMILIES: tuple[str, ...] = tuple(
    f"{a}-{b}-{orient}"
    for i, a in enumerate(EDGES)
    for b in EDGES[i:]
    for orient in ORIENTATIONS
)

#: Shorthand a designer is likely to type, to its family. ``WC`` and
#: ``wobble`` are both ``W-W-cis`` — a G·U wobble is a Watson-Crick-edge
#: cis pair geometrically, it is only the *base* combination that differs,
#: which is exactly what :data:`ALLOWED_PAIRS` distinguishes.
GEOMETRY_ALIASES = {
    "wc": "W-W-cis",
    "watson-crick": "W-W-cis",
    "wobble": "W-W-cis",
    "hoogsteen": "W-H-cis",
    "reverse-hoogsteen": "W-H-trans",
    "sugar": "W-S-cis",
    "mismatch": "W-W-trans",
}

#: Which base combinations each family is known to accommodate, as
#: ORDERED pairs ``(subject_edge_base, object_edge_base)`` with RNA
#: lettering (T is folded onto U by :func:`canonical_base`).
#:
#: **Curated, not exhaustive.** The sources (Leontis & Westhof 2001; the
#: isostericity matrices of Leontis, Stombaugh & Westhof 2002) enumerate
#: the *frequent* occupants of each family over solved RNA structures;
#: rare occupants exist. The consequence for the DRC rule that reads this
#: table (``chain_pairing_geometry``) is stated in its finding text: the
#: error says the declared family does not accommodate those bases **per
#: this table**, and the fix is either the bases or the declaration. The
#: table is deliberately generous where a family is symmetric in its two
#: edges (both orders are listed) and deliberately silent about the
#: families for which no common occupancy is coded — those accept
#: nothing, so a design declaring one is asked to say what it means.
ALLOWED_PAIRS: dict[str, frozenset[tuple[str, str]]] = {
    # Watson-Crick + the G·U/U·G wobble, which shares the family.
    "W-W-cis": frozenset(
        {
            ("A", "U"),
            ("U", "A"),
            ("G", "C"),
            ("C", "G"),
            ("G", "U"),
            ("U", "G"),
        }
    ),
    # The trans-WC family: the reverse-WC pairs, incl. the A·U and G·C
    # reverse pairs and the symmetric purine/pyrimidine self-pairs.
    "W-W-trans": frozenset(
        {
            ("A", "U"),
            ("U", "A"),
            ("G", "C"),
            ("C", "G"),
            ("A", "A"),
            ("C", "C"),
            ("U", "U"),
            ("G", "G"),
            ("A", "C"),
            ("C", "A"),
        }
    ),
    # Hoogsteen: the purine offers its H edge.
    "W-H-cis": frozenset({("U", "A"), ("C", "G"), ("A", "A"), ("G", "G")}),
    # Reverse Hoogsteen — the U·A of the U·A·U triple, and the purine
    # self-pairs. Notably NOT C·C: neither cytosine has a Hoogsteen edge
    # to offer that geometry.
    "W-H-trans": frozenset(
        {("U", "A"), ("A", "U"), ("A", "A"), ("G", "G"), ("G", "A"), ("A", "G")}
    ),
    # Sugar-edge families: the common sheared and ribose-mediated pairs.
    "W-S-cis": frozenset({("A", "G"), ("G", "A"), ("C", "G"), ("U", "G")}),
    "W-S-trans": frozenset({("A", "G"), ("G", "A"), ("U", "G"), ("G", "U")}),
    "H-H-cis": frozenset({("A", "A"), ("G", "G")}),
    "H-H-trans": frozenset({("A", "A"), ("G", "G")}),
    "H-S-cis": frozenset({("A", "G"), ("G", "A")}),
    "H-S-trans": frozenset({("A", "G"), ("G", "A"), ("A", "A")}),
    # The sheared A·G / G·A pair and its family-mates.
    "S-S-cis": frozenset({("A", "G"), ("G", "A"), ("G", "G"), ("A", "A")}),
    "S-S-trans": frozenset({("G", "G"), ("A", "A")}),
}

#: The four bases, after :func:`canonical_base` folds T onto U.
BASES = ("A", "C", "G", "U")


def canonical_base(letter: str) -> str | None:
    """One sequence letter, upper-cased with ``T`` folded onto ``U`` —
    ``None`` for anything that is not a base (``N``, a gap, whitespace).

    T→U because the Leontis–Westhof geometry is about edges, and thymine
    presents the same edges as uracil; the 5-methyl is not a pairing
    feature. The letter distinction is preserved in the stored sequence —
    only this lookup folds it.
    """
    up = letter.strip().upper()
    if up == "T":
        return "U"
    return up if up in BASES else None


def canonical_geometry(raw: str) -> str | None:
    """A declared pair geometry, canonicalised — an alias resolved
    (:data:`GEOMETRY_ALIASES`) and the edge pair put in
    :data:`EDGES` order. ``None`` when it is not one of the 12 families."""
    text = raw.strip()
    alias = GEOMETRY_ALIASES.get(text.lower())
    if alias is not None:
        return alias
    parts = text.split("-")
    if len(parts) != 3:
        return None
    first, second, orient = parts[0].upper(), parts[1].upper(), parts[2].lower()
    if first not in EDGES or second not in EDGES or orient not in ORIENTATIONS:
        return None
    if EDGES.index(second) < EDGES.index(first):
        first, second = second, first
    family = f"{first}-{second}-{orient}"
    return family if family in FAMILIES else None


def pair_allowed(geometry: str, a: str, b: str) -> bool:
    """Can bases ``a`` and ``b`` pair in family ``geometry``, per
    :data:`ALLOWED_PAIRS`?

    Either order is accepted: which of the two bases is the "subject" is a
    property of the *domain* that declared the geometry, not of the
    chemistry, and the table lists a family's pairs in both orders
    wherever the family is symmetric.
    """
    family = canonical_geometry(geometry)
    if family is None:
        return False
    base_a, base_b = canonical_base(a), canonical_base(b)
    if base_a is None or base_b is None:
        # An 'N' (or any non-base) is a deliberate don't-know; a geometry
        # declaration over it is unverifiable, not wrong.
        return True
    allowed = ALLOWED_PAIRS.get(family, frozenset())
    return (base_a, base_b) in allowed or (base_b, base_a) in allowed


#: Watson-Crick complements, for the derived-pairing readout.
COMPLEMENT_DNA = {"A": "T", "T": "A", "G": "C", "C": "G"}
COMPLEMENT_RNA = {"A": "U", "U": "A", "G": "C", "C": "G"}
