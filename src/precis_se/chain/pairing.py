"""Derived pairing — who occupies each helix offset, and what that means.

**Pairing is never declared.** A base pair exists because two strands
occupy the same helix offset running opposite ways; that is the whole rule
(this domain's decomposition, :mod:`precis_se.chain`). The
consequences fall out of it rather than needing their own vocabulary:

- exactly two occupants, antiparallel → a **pair**;
- exactly one → **single-stranded** (a foothold, a toehold, an ssDNA
  scaffold region — all the same thing at this altitude);
- two occupants running the same way, or three or more → a
  ``chain_occupancy`` finding. Triplexes are out of scope for this cut
  (they need a third backbone azimuth), so this reports them rather than
  modelling them;
- two antiparallel occupants where a domain marks the offset ``unpaired``
  (the ``unpair`` op) → **unpaired**: occupied by both strands, a base pair
  by neither's account — kept out of ``pairs`` so no pairing check applies.

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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from precis_se.chain import nucleic
from precis_se.chain.occupancy import apply_occupancy
from precis_se.chain.vocab import (
    STRAND_ROLE,
    DomainSpec,
    chain_role,
    group_domains,
)
from precis_se.chain.vocab import (
    UNPAIRED as UNPAIRED_MARK,
)

#: An offset's occupancy verdict.
PAIRED = "paired"
SINGLE = "single"
PARALLEL = "parallel"
CROWDED = "crowded"
UNPAIRED = "unpaired"


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
    #: Occupants whose domain marks this offset ``unpaired``
    #: (``overrides[offset] == "unpaired"``, the ``unpair`` op). Non-empty
    #: on two antiparallel occupants makes the status ``unpaired``; on
    #: anything else it is a stray mark (``chain_unpaired_stray``).
    unpaired_by: tuple[Occupant, ...] = ()


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
    unpaired: list[OffsetOccupancy] = field(default_factory=list)

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


@dataclass(frozen=True)
class HelixIndel:
    """One helix's register insertions/deletions, as the sequence accounting
    reads them. ``deletions`` are offsets holding no base; ``insertions``
    maps an offset to how many extra bases it carries (caDNAno's loop
    count)."""

    deletions: frozenset[int] = frozenset()
    insertions: Mapping[int, int] = field(default_factory=dict)


def helix_indels(tree: Any) -> dict[str, HelixIndel]:
    """``{helix: HelixIndel}`` for every helix block whose ``register``
    lists a deletion or insertion. Reads the stored lists defensively — a
    malformed record is the DRC's ``chain_malformed`` to report, not a crash
    here."""
    out: dict[str, HelixIndel] = {}
    for name, node in tree.blocks.items():
        record = getattr(node, "chain", None)
        if not isinstance(record, dict):
            continue
        register = record.get("register")
        if not isinstance(register, dict):
            continue
        deletions = frozenset(
            v for v in register.get("deletions") or [] if isinstance(v, int)
        )
        insertions: dict[int, int] = {}
        for v in register.get("insertions") or []:
            if isinstance(v, int):
                insertions[v] = insertions.get(v, 0) + 1
        if deletions or insertions:
            out[name] = HelixIndel(deletions, insertions)
    return out


def strand_letters(
    sequence: str | None,
    route: list[DomainSpec],
    indels: Mapping[str, HelixIndel] | None = None,
) -> dict[tuple[int, int], str]:
    """Map ``(ord, offset)`` → sequence letter for one strand's route.

    A strand's sequence runs 5'→3' over **everything it contains**: each
    domain's offsets in traversal order, with the ``loop_before_nt``
    nucleotides of the loop that precedes a domain consuming their own
    letters in between. Getting that accounting wrong would shift every
    letter after the first loop, which is why the loops are counted here
    rather than by each caller.

    ``indels`` carries each helix's register insertions/deletions: a deleted
    offset holds no base — it consumes no letter and gets none — and an
    inserted offset with ``k`` extra bases consumes ``1 + k`` letters. Which
    of those letters pairs is not knowable from the offset alone, so an
    inserted offset gets **no** entry: its letters read as unverifiable
    (:func:`watson_crick` returns ``None``) rather than risking a false
    mismatch.

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
        indel = (indels or {}).get(domain.helix)
        for offset in domain.offsets():
            if indel is not None and offset in indel.deletions:
                continue
            extra = indel.insertions.get(offset, 0) if indel is not None else 0
            if cursor >= len(sequence):
                return out
            if not extra:
                out[(domain.ord, offset)] = sequence[cursor]
            cursor += 1 + extra
    return out


def strand_length_nt(
    route: list[DomainSpec], indels: Mapping[str, HelixIndel] | None = None
) -> int:
    """How many nucleotides a route accounts for — every domain's units
    (less its deleted offsets, plus its inserted bases) plus every loop's,
    the number a declared sequence's length is checked against."""
    total = 0
    for d in route:
        total += d.n_units + (d.loop_before_nt or 0)
        indel = (indels or {}).get(d.helix)
        if indel is not None:
            total -= sum(1 for o in d.offsets() if o in indel.deletions)
            total += sum(indel.insertions.get(o, 0) for o in d.offsets())
    return total


def derive_pairing(tree: Any, state: Mapping[str, str | None] | None = None) -> Pairing:
    """Derive the whole design's occupancy from its domain rows.

    ``state`` is a walker state's **occupancy map** — ``{"<strand>.<ord>":
    "<helix>@<offset>" | None}`` (:mod:`precis_se.chain.occupancy`) —
    applied to a copy of the rows before grouping, so the pairing is the
    one that holds in that state: a moved leg domain pairs where its
    foothold is, a ``None`` (free) leg pairs nowhere. ``None``/empty reads
    the rows as declared. The handler's ``args={'state': ...}`` path
    applies the same map to the tree itself instead
    (:func:`precis_se.handler._apply_state_arg`), which is why every
    other chain consumer needs no ``state`` of its own.
    """
    domains = list(getattr(tree, "domains", []) or [])
    if state:
        domains = apply_occupancy(domains, dict(state))
    tables = group_domains(domains)
    indels = helix_indels(tree)
    letters: dict[str, dict[tuple[int, int], str]] = {}
    for strand, route in tables.by_strand.items():
        node = tree.blocks.get(strand)
        sequence = None
        if node is not None and chain_role(node) == STRAND_ROLE:
            sequence = (node.chain or {}).get("sequence")
        letters[strand] = strand_letters(sequence, route, indels)

    raw: dict[tuple[str, int], list[Occupant]] = {}
    geometry: dict[tuple[str, int], str] = {}
    declarations: dict[tuple[str, int], list[tuple[Occupant, str]]] = {}
    unpaired_by: dict[tuple[str, int], list[Occupant]] = {}
    all_domains = sorted(
        (d for route in tables.by_strand.values() for d in route),
        key=lambda d: (d.strand, d.ord),
    )
    for domain in all_domains:
        overrides = domain.overrides or {}
        deleted = indels[domain.helix].deletions if domain.helix in indels else ()
        for offset in domain.offsets():
            if offset in deleted:
                continue  # a deleted offset holds no base to occupy it
            key = (domain.helix, offset)
            occupant = Occupant(
                strand=domain.strand,
                ord=domain.ord,
                forward=domain.forward,
                letter=letters.get(domain.strand, {}).get((domain.ord, offset)),
            )
            raw.setdefault(key, []).append(occupant)
            override = overrides.get(str(offset))
            if override == UNPAIRED_MARK:
                # Not a geometry declaration — and the domain's whole-domain
                # geometry does not reach an offset it says is not paired.
                unpaired_by.setdefault(key, []).append(occupant)
                continue
            declared = override or domain.geometry
            if declared is not None:
                if key not in geometry:
                    geometry[key] = declared
                family = nucleic.canonical_geometry(declared) or declared
                declarations.setdefault(key, []).append((occupant, family))

    out = Pairing()
    for key in sorted(raw):
        helix, offset = key
        occupants = tuple(raw[key])
        marks = tuple(unpaired_by.get(key, []))
        if len(occupants) == 1:
            status = SINGLE
        elif len(occupants) == 2:
            if occupants[0].forward == occupants[1].forward:
                status = PARALLEL
            else:
                status = UNPAIRED if marks else PAIRED
        else:
            status = CROWDED
        record = OffsetOccupancy(
            helix=helix,
            offset=offset,
            occupants=occupants,
            status=status,
            geometry=geometry.get(key),
            declarations=tuple(declarations.get(key, [])),
            unpaired_by=marks,
        )
        out.offsets[key] = record
        if status == PAIRED:
            out.pairs.append(record)
        elif status == UNPAIRED:
            out.unpaired.append(record)
        elif status == SINGLE:
            out.singles.append(record)
        else:
            out.conflicts.append(record)
    return out


def watson_crick(occ: OffsetOccupancy) -> bool | None:
    """Do a paired offset's two letters complement each other?

    ``None`` when either letter is missing (unsequenced, or an ``N``) —
    unverifiable, which is different from wrong and is reported as such.

    Both letters go through :func:`precis_se.chain.nucleic.canonical_base`
    (T folded onto U) before the comparison, so a DNA strand's ``T``
    against an RNA strand's ``A`` is the complement it is: a strand's
    alphabet is its own, and a hybrid duplex pairs across alphabets.
    """
    if occ.status != PAIRED:
        return None
    a, b = occ.occupants[0].letter, occ.occupants[1].letter
    if not a or not b:
        return None
    base_a, base_b = nucleic.canonical_base(a), nucleic.canonical_base(b)
    if base_a is None or base_b is None:
        return None  # 'N' (or any non-base) is a deliberate don't-know
    return nucleic.COMPLEMENT_RNA.get(base_a) == base_b
