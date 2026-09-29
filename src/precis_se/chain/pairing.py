"""Derived pairing — who occupies each helix offset, and what that means.

**Pairing is never declared.** A base pair exists because two strands
occupy the same helix offset running opposite ways; that is the whole rule
(docs/backlog/se-nucleic-acid.md's decomposition, chosen 2026-09-27). The
consequences fall out of it rather than needing their own vocabulary:

- exactly two occupants, antiparallel → a **pair**;
- exactly one → **single-stranded** (a foothold, a toehold, an ssDNA
  scaffold region — all the same thing at this altitude);
- two occupants running the same way, or three or more → a
  ``chain_occupancy`` finding. Triplexes are out of scope for this cut
  (they need a third backbone azimuth), so this reports them rather than
  modelling them.

**Cost is O(total domain length)**, not O(n²): each domain writes its
offsets into one dict and nothing ever compares two domains directly. A
7 kb scaffold plus its staples is ~14 000 dict writes.

The ``state=`` kwarg on :func:`derive_pairing` is a **no-op today** and
exists from day one so ``se-walker-light-protocol`` — which will ask "who
occupies this offset *in this walker state*" — never has to change the
signature. Passing anything but ``None`` raises rather than being quietly
ignored: an unhonoured facet that returns unfiltered results is the
``**_kw`` failure mode this repo has been bitten by
(``search_facets_swallowed``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis_se.chain import nucleic
from precis_se.chain.vocab import STRAND_ROLE, DomainSpec, chain_role, group_domains

#: An offset's occupancy verdict.
PAIRED = "paired"
SINGLE = "single"
PARALLEL = "parallel"
CROWDED = "crowded"


@dataclass(frozen=True)
class Occupant:
    """One strand's presence at one helix offset."""

    strand: str
    ord: int
    forward: bool
    #: The sequence letter at this offset, when the strand carries a
    #: sequence — ``None`` for an unsequenced design, which is a normal and
    #: common state (a 24-helix rectangle is a real design long before its
    #: 7 kb of sequence exists).
    letter: str | None = None


@dataclass(frozen=True)
class OffsetOccupancy:
    """Everything derived about one ``(helix, offset)``."""

    helix: str
    offset: int
    occupants: tuple[Occupant, ...]
    status: str
    #: The declared Leontis–Westhof family in force at this offset — a
    #: domain's ``overrides[offset]`` if it has one, else its ``geometry``.
    #: The first declaring occupant wins, in occupant order — this field's
    #: reading never changes when a second domain disagrees, so every
    #: existing consumer (the realizer, ``view='chain'``) keeps its answer.
    #: :data:`declarations` below is what lets a disagreement be *seen*
    #: without changing what wins (``drc.py``'s ``chain_pairing_disagree``).
    geometry: str | None = None
    #: Every declaring occupant's own reading, in the same occupant order
    #: as ``occupants`` — ``(occupant, family)`` with ``family``
    #: canonicalised through :func:`precis_se.chain.nucleic.canonical_geometry`
    #: so ``WC`` and ``W-W-cis`` are the same declaration, not a
    #: disagreement. ``declarations[0]`` is always the occupant
    #: ``geometry`` above came from — the first to declare.
    declarations: tuple[tuple[Occupant, str], ...] = ()


@dataclass
class Pairing:
    """The whole design's derived occupancy.

    ``offsets`` is keyed ``(helix, offset)``; the three lists are views of
    it, kept so a consumer does not re-scan (the DRC pass wants the
    conflicts, ``view='chain'`` wants the counts, the realizer wants the
    map).
    """

    offsets: dict[tuple[str, int], OffsetOccupancy] = field(default_factory=dict)
    pairs: list[OffsetOccupancy] = field(default_factory=list)
    singles: list[OffsetOccupancy] = field(default_factory=list)
    conflicts: list[OffsetOccupancy] = field(default_factory=list)

    def at(self, helix: str, offset: int) -> OffsetOccupancy | None:
        return self.offsets.get((helix, offset))

    def single_runs(self) -> list[tuple[str, int, int]]:
        """Contiguous single-occupancy runs as ``(helix, first, last)``,
        inclusive — what ``chain_floppy`` measures and what a realizer
        needs to know is not a duplex."""
        runs: list[tuple[str, int, int]] = []
        for occ in sorted(self.singles, key=lambda o: (o.helix, o.offset)):
            if runs and runs[-1][0] == occ.helix and runs[-1][2] == occ.offset - 1:
                helix, first, _last = runs[-1]
                runs[-1] = (helix, first, occ.offset)
            else:
                runs.append((occ.helix, occ.offset, occ.offset))
        return runs


def strand_letters(
    sequence: str | None, route: list[DomainSpec]
) -> dict[tuple[int, int], str]:
    """Map ``(ord, offset)`` → sequence letter for one strand's route.

    A strand's sequence runs 5'→3' over **everything it contains**: each
    domain's offsets in traversal order, with the ``loop_before_nt``
    nucleotides of the loop that precedes a domain consuming their own
    letters in between. Getting that accounting wrong would shift every
    letter after the first loop, which is why the loops are counted here
    rather than by each caller.

    A sequence shorter than the route it describes simply runs out — the
    remaining offsets get no letter, and the DRC pass reports the shortfall
    (a wrong-length sequence is a finding, never a crash).
    """
    if not sequence:
        return {}
    out: dict[tuple[int, int], str] = {}
    cursor = 0
    for domain in route:
        cursor += domain.loop_before_nt or 0
        for offset in domain.offsets():
            if cursor >= len(sequence):
                return out
            out[(domain.ord, offset)] = sequence[cursor]
            cursor += 1
    return out


def strand_length_nt(route: list[DomainSpec]) -> int:
    """How many nucleotides a route accounts for — every domain's units
    plus every loop's, the number a declared sequence's length is checked
    against."""
    return sum(d.n_units + (d.loop_before_nt or 0) for d in route)


def derive_pairing(tree: Any, state: Any = None) -> Pairing:
    """Derive the whole design's occupancy from its domain rows.

    ``state`` is reserved for ``se-walker-light-protocol`` (module
    docstring) and must be ``None`` here.
    """
    if state is not None:
        raise NotImplementedError(
            "derive_pairing(state=) is a reserved hook for "
            "se-walker-light-protocol's occupancy states — no consumer fills "
            "it yet, and honouring it silently would report unfiltered "
            "occupancy as if it were state-filtered"
        )
    tables = group_domains(list(getattr(tree, "domains", []) or []))
    letters: dict[str, dict[tuple[int, int], str]] = {}
    for strand, route in tables.by_strand.items():
        node = tree.blocks.get(strand)
        sequence = None
        if node is not None and chain_role(node) == STRAND_ROLE:
            sequence = (node.chain or {}).get("sequence")
        letters[strand] = strand_letters(sequence, route)

    raw: dict[tuple[str, int], list[Occupant]] = {}
    geometry: dict[tuple[str, int], str] = {}
    declarations: dict[tuple[str, int], list[tuple[Occupant, str]]] = {}
    all_domains = sorted(
        (d for route in tables.by_strand.values() for d in route),
        key=lambda d: (d.strand, d.ord),
    )
    for domain in all_domains:
        overrides = domain.overrides or {}
        for offset in domain.offsets():
            key = (domain.helix, offset)
            occupant = Occupant(
                strand=domain.strand,
                ord=domain.ord,
                forward=domain.forward,
                letter=letters.get(domain.strand, {}).get((domain.ord, offset)),
            )
            raw.setdefault(key, []).append(occupant)
            declared = overrides.get(str(offset)) or domain.geometry
            if declared is not None:
                if key not in geometry:
                    geometry[key] = declared
                family = nucleic.canonical_geometry(declared) or declared
                declarations.setdefault(key, []).append((occupant, family))

    out = Pairing()
    for key in sorted(raw):
        helix, offset = key
        occupants = tuple(raw[key])
        if len(occupants) == 1:
            status = SINGLE
        elif len(occupants) == 2:
            status = (
                PAIRED if occupants[0].forward != occupants[1].forward else PARALLEL
            )
        else:
            status = CROWDED
        record = OffsetOccupancy(
            helix=helix,
            offset=offset,
            occupants=occupants,
            status=status,
            geometry=geometry.get(key),
            declarations=tuple(declarations.get(key, [])),
        )
        out.offsets[key] = record
        if status == PAIRED:
            out.pairs.append(record)
        elif status == SINGLE:
            out.singles.append(record)
        else:
            out.conflicts.append(record)
    return out


def watson_crick(occ: OffsetOccupancy, nucleic_name: str = "DNA") -> bool | None:
    """Do a paired offset's two letters complement each other?

    ``None`` when either letter is missing (unsequenced, or an ``N``) —
    unverifiable, which is different from wrong and is reported as such.
    """
    if occ.status != PAIRED:
        return None
    a, b = occ.occupants[0].letter, occ.occupants[1].letter
    if not a or not b:
        return None
    table = nucleic.COMPLEMENT_RNA if nucleic_name == "RNA" else nucleic.COMPLEMENT_DNA
    return table.get(a.upper()) == b.upper()
