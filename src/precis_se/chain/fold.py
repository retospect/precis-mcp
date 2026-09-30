"""The ViennaRNA seam — ``fold_layout`` (a fold turned into helix/strand/
domain records) and the three fold findings.

**Lazy import, everywhere.** ViennaRNA ships in the optional ``[chain]``
extra (``pyproject.toml``), so ``import RNA`` lives inside
:func:`rna_module` and never at module top: a venv without the extra must
still boot every other ``se`` view, and ``view='drc'`` must still render.
The op raises :class:`~precis.errors.Unsupported` naming the install; the
DRC pass degrades to one ``chain_fold_unavailable`` info row for the whole
design and keeps the off-target check, which needs no library at all.

**ViennaRNA's parameters are RNA's.** There is no DNA parameter set in
play here (loading one is global process state in RNAlib, which a handler
cannot safely mutate), so a DNA strand is folded under the Turner RNA model
with ``T`` read as ``U``. That is an approximation and every message that
carries a fold says so — a ``chain_fold_disagree`` warn on a DNA design is
a hint to look, never a verdict.

**A dot-bracket is a pairing, not a layout.** :func:`stacks` turns the
matched brackets into maximal stacks (one stack = one helix: consecutive
``i+1``/``j-1`` pairs), and each stack becomes ONE helix block carrying two
antiparallel domains of the same strand — the item's own decomposition of a
hairpin. The unpaired stretches between consecutive domains become
``loop_before_nt``; an unpaired 5'/3' tail becomes a **single-occupancy
stub helix** (``<strand>.t5``/``.t3``) carrying the strand's first/last
domain, which is what :func:`precis_se.chain.vocab.build_domain` already
says a 5' overhang is. Shapes covered and refused are
:func:`op_fold_layout`'s docstring; the placement it writes is **nominal**
and exists to be settled by ``relax_chain``, never as a claim about shape:
each helix straight along ``±z``, a helix the strand reaches through at
least one unpaired nucleotide on every crossing stacked along ``+x`` one
:data:`precis_se.chain.nucleic.HELIX_SPACING_M` apart, and a helix reached
through ZERO unpaired nucleotides on some crossing (a bulge, a one-sided
internal loop, a coaxial stack in a multiloop, a tail stub) placed **end
to end** on the helix it stacks on, continuing its axis with the twist
phase chosen so the two backbone exits meet (:func:`_stacked`).

**Cost.** ``RNA.fold`` is O(n³), so the *checks* are bounded to strands
≤ :data:`MAX_CHECK_NT`; a longer strand is reported skipped **with its
length**, because a 6 kb scaffold silently unchecked reads as a clean one.
``fold_layout`` itself takes scaffold-length input (the item allows it
there and only there, human-Apply gated) up to :data:`MAX_LAYOUT_NT`. The
off-target scan is a 6-mer hash index over every strand —
O(total length + hits), never a pairwise alignment.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from precis.errors import Unsupported
from precis.utils.units import format_quantity
from precis_se.chain import layout as chain_layout
from precis_se.chain import nucleic
from precis_se.chain import vocab as chain_vocab
from precis_se.chain.layout import HelixGeometry
from precis_se.chain.pairing import PAIRED, derive_pairing
from precis_se.chain.vocab import STRAND_ROLE, ChainError, DomainSpec, chain_role
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.validate import ValidationIssue

#: Keys ``fold_layout`` accepts.
_ALLOWED_KEYS = frozenset({"op", "strand", "sequence", "nucleic", "parent"})

#: Upper bound on a strand the DRC pass will fold. ``RNA.fold`` is O(n³);
#: 200 nt is the item's own number and folds in a few milliseconds, where a
#: 6 kb scaffold would take minutes inside a ``view='drc'`` render.
MAX_CHECK_NT = 200

#: Upper bound on ``fold_layout``'s own input. Scaffold length is allowed
#: here (the op is human-Apply gated and spending compute is its job), but
#: not unbounded: at O(n³) a 10 kb fold is already ~minutes and anything
#: past it is a typo, not a design.
MAX_LAYOUT_NT = 10_000

#: The off-target index's window. 6 is the granularity, not the reporting
#: threshold — the index is what makes the scan O(total length).
OFFTARGET_SEED_NT = 6

#: The shortest unintended complementary run the pass reports. A 6-mer
#: recurs by chance roughly every 4 kb² of pairwise sequence, so reporting
#: at the index's own granularity would bury the report in noise; 8 nt is
#: the item's own criterion ("a planted 8-nt staple–staple complement").
OFFTARGET_MIN_NT = 8

#: How many off-target rows the report carries before it summarises the
#: rest in one row — a repetitive 6 kb design has hundreds of chance
#: 8-mers, and a table that long is not read.
MAX_OFFTARGET_ROWS = 10

#: The install line every unavailable path names.
INSTALL_HINT = "pip install 'precis-mcp[chain]'"

#: What a fold under RNA parameters is, said once.
RNA_PARAMS_NOTE = "ViennaRNA's RNA parameters (T read as U) — an approximation for DNA"

#: Watson-Crick complements in RNA lettering, the only complementarity the
#: off-target index looks for. A G·U wobble pairs in ``W-W-cis`` too, but
#: it does not make an off-target duplex on its own.
_RC = {"A": "U", "U": "A", "G": "C", "C": "G"}


def rna_module() -> Any | None:
    """The ``RNA`` module, or ``None`` when the ``[chain]`` extra is not
    installed — the one import seam (module docstring), so both the op's
    refusal and the DRC pass's degradation read the same answer, and a test
    can simulate an absent library by making the import fail."""
    try:
        import RNA
    except ImportError:
        return None
    return RNA


def as_rna(sequence: str) -> str:
    """A stored sequence in ViennaRNA's alphabet — upper case with ``T``
    folded onto ``U`` (:func:`precis_se.chain.nucleic.canonical_base`'s own
    rule)."""
    return sequence.upper().replace("T", "U")


def mfe_fold(rna: Any, sequence: str) -> tuple[str, float]:
    """``(dot-bracket, MFE in kcal/mol)`` for one sequence."""
    structure, energy = rna.fold(as_rna(sequence))
    return str(structure), float(energy)


def pair_table(structure: str) -> dict[int, int]:
    """A dot-bracket as ``{i: j}`` both ways, 0-based over the sequence.

    Raises :class:`~precis_se.chain.vocab.ChainError` on an unbalanced or
    non-nested string — ViennaRNA's MFE never emits one, and a hand-passed
    structure that is pseudoknotted has no representation in this model.
    """
    stack: list[int] = []
    out: dict[int, int] = {}
    for i, char in enumerate(structure):
        if char == "(":
            stack.append(i)
        elif char == ")":
            if not stack:
                raise ChainError(
                    f"dot-bracket {structure!r} closes a pair at position {i} "
                    "that was never opened"
                )
            j = stack.pop()
            out[j], out[i] = i, j
        elif char != ".":
            raise ChainError(
                f"dot-bracket {structure!r} has character {char!r} at "
                f"position {i} — only '(', ')' and '.' are a secondary "
                "structure in this model"
            )
    if stack:
        raise ChainError(f"dot-bracket {structure!r} leaves {len(stack)} pair(s) open")
    return out


def stacks(pairs: dict[int, int]) -> list[list[tuple[int, int]]]:
    """The pair table's maximal stacks, 5'→3' — each one helix.

    A stack continues while both strands advance by exactly one
    (``i+1``/``j-1``); a bulge or an internal loop therefore *ends* a
    stack, which is why the two helices either side of one are separate
    blocks — placed end to end by :func:`op_fold_layout` when the strand
    crosses between them with zero unpaired nucleotides on some side.
    """
    out: list[list[tuple[int, int]]] = []
    for i in sorted(pairs):
        j = pairs[i]
        if j < i:
            continue
        if out and out[-1][-1] == (i - 1, j + 1):
            out[-1].append((i, j))
        else:
            out.append([(i, j)])
    return out


@dataclass(frozen=True)
class _Entry:
    """One domain the fold implies: a sequence range, the stack it lies on,
    and which way the strand runs through it."""

    start: int  # first sequence position, inclusive
    end: int  # last sequence position, inclusive
    helix: int  # index into :func:`stacks`'s list
    forward: bool


def _entries(stack_list: list[list[tuple[int, int]]]) -> list[_Entry]:
    """The paired domains of a fold, in 5'→3' order — two per stack (the
    strand runs up one side and back down the other), sorted by where they
    start. :func:`_helices_and_entries` adds the tail stubs' domains so the
    whole list tiles the sequence exactly, which is what makes the loop
    arithmetic (and the stored sequence's own length accounting) exact
    rather than approximate.
    """
    out: list[_Entry] = []
    for k, stack in enumerate(stack_list):
        out.append(_Entry(stack[0][0], stack[-1][0], k, True))
        out.append(_Entry(stack[-1][1], stack[0][1], k, False))
    return sorted(out, key=lambda e: e.start)


@dataclass(frozen=True)
class _Helix:
    """One helix block the fold implies: ``paired`` for a stack (a duplex
    carrying two domains of the strand), ``False`` for an unpaired 5'/3'
    tail — a single-occupancy stub the strand's first or last domain sits
    on. The stub is the tail's record: the strand's route finds it as a
    domain on a helix nobody else occupies (``view='chain'`` counts it
    single-stranded), and a realizer finds it the same way."""

    label: str  # ``h<k>`` for stack k, ``t5``/``t3`` for a tail
    n_units: int
    paired: bool


def _helices_and_entries(
    stack_list: list[list[tuple[int, int]]], n_seq: int
) -> tuple[list[_Helix], list[_Entry]]:
    """Every helix the fold needs and every domain on them, 5'→3' — the
    stacks first, then a ``t5``/``t3`` stub for each unpaired tail."""
    helices = [_Helix(f"h{k}", len(stack), True) for k, stack in enumerate(stack_list)]
    entries = _entries(stack_list)
    if entries[0].start > 0:
        helices.append(_Helix("t5", entries[0].start, False))
        entries.insert(0, _Entry(0, entries[0].start - 1, len(helices) - 1, True))
    if entries[-1].end < n_seq - 1:
        helices.append(_Helix("t3", n_seq - 1 - entries[-1].end, False))
        entries.append(_Entry(entries[-1].end + 1, n_seq - 1, len(helices) - 1, True))
    return helices, entries


@dataclass(frozen=True)
class _Placement:
    """A nominal helix placement: unit 0's axis point (m), the unit
    direction (a unit vector) and the twist phase at unit 0 (rad)."""

    origin: np.ndarray
    axis: np.ndarray
    phase0: float


def _path(placement: _Placement, n_units: int, rise: float) -> dict[str, Any]:
    """A straight centre line from the placement, one rise per unit. Units
    are explicit because :mod:`precis_se.chain.vocab` refuses a bare
    number, the same as a human-authored ``declare_helix``."""
    length = max(n_units - 1, 1) * rise
    a = placement.origin
    b = placement.origin + placement.axis * length
    return {
        "waypoints": [
            [f"{float(v)} m" for v in a],
            [f"{float(v)} m" for v in b],
        ]
    }


def _junctions(
    entries: list[_Entry],
) -> tuple[dict[frozenset[int], int], set[frozenset[int]]]:
    """Per pair of helices the strand crosses between directly: the
    unpaired nucleotides on all its crossings summed (a bulge's size), and
    the set of pairs some crossing joins with ZERO unpaired nucleotides —
    the coaxial ones. A hairpin loop closes one helix and joins nothing."""
    total: dict[frozenset[int], int] = {}
    coaxial: set[frozenset[int]] = set()
    for before, after in itertools.pairwise(entries):
        if before.helix == after.helix:
            continue
        key = frozenset((before.helix, after.helix))
        gap = after.start - before.end - 1
        total[key] = total.get(key, 0) + gap
        if gap == 0:
            coaxial.add(key)
    return total, coaxial


def _geometry(
    helix: _Helix, placement: _Placement, nucleic_name: str, motif_name: str
) -> HelixGeometry:
    """The geometry ``declare_helix`` would realise for this placement —
    built from the same record, so the backbone exits the placement is
    tuned against are the ones ``chain_loop_short`` will measure."""
    rise = nucleic.MOTIFS[motif_name].rise
    record = chain_vocab.build_helix(
        {
            "n_units": helix.n_units,
            "nucleic": nucleic_name,
            "motif": motif_name,
            "path": _path(placement, helix.n_units, rise),
            "phase0": f"{placement.phase0} rad",
        },
        what="fold_layout",
    )
    return chain_layout.helix_geometry(SimpleNamespace(name=helix.label, chain=record))


def _exit_unit(helix: _Helix, entry: _Entry) -> int:
    """The unit the strand leaves ``helix`` at — the last on the forward
    strand, the first on the reverse."""
    return helix.n_units - 1 if entry.forward else 0


def _entry_unit(helix: _Helix, entry: _Entry) -> int:
    return 0 if entry.forward else helix.n_units - 1


def _stacked(
    anchor: _Placement,
    anchor_helix: _Helix,
    anchor_entry: _Entry,
    helix: _Helix,
    entry: _Entry,
    extra_nt: int,
    *,
    anchor_leaves: bool,
    nucleic_name: str,
    motif_name: str,
) -> _Placement:
    """Place ``helix`` end to end on the already-placed ``anchor_helix``
    across a 0-nt crossing between them — what a coaxial stack physically
    is. ``anchor_leaves`` says which way the strand crosses: ``True`` when
    it leaves the anchor (at ``anchor_entry``'s exit unit) and enters
    ``helix`` at ``entry``'s entry unit, ``False`` when it leaves ``helix``
    and enters the anchor — the closing crossing of a multiloop reaches
    the outer helix, which was placed first.

    The strand's travel continues straight through the crossing (the new
    axis is that travel, flipped when the strand runs the new helix in
    reverse); the two units it crosses between sit one rise apart plus one
    rise per bulged nucleotide (``extra_nt`` — a bulge is a short axial
    offset with the bulged nucleotides as an unpaired bubble, no loop
    record needed); and the twist phase is the one that brings the two
    backbone exits together, so the crossing reads as a backbone step and
    not a crossover. The phase is analytic — a unit's backbone exit lies on
    a circle about the axis, so the phase is the azimuth of the anchor's
    backbone point on that circle — and the sense of ``phase0`` is settled
    by building both signs and keeping the closer.
    """
    rise = nucleic.MOTIFS[motif_name].rise
    travel = anchor.axis if anchor_entry.forward else -anchor.axis
    if anchor_leaves:
        anchor_unit = _exit_unit(anchor_helix, anchor_entry)
        unit = _entry_unit(helix, entry)
    else:
        anchor_unit = _entry_unit(anchor_helix, anchor_entry)
        unit = _exit_unit(helix, entry)
    step = travel * (rise * (1 + extra_nt))
    anchor_point = anchor.origin + anchor.axis * (rise * anchor_unit)
    point = anchor_point + step if anchor_leaves else anchor_point - step
    axis = travel if entry.forward else -travel
    origin = point - axis * (rise * unit)
    target = _geometry(anchor_helix, anchor, nucleic_name, motif_name).exit(
        anchor_unit, anchor_entry.forward
    )
    radial = target - point
    radial = radial - axis * float(np.dot(radial, axis))
    trial = _Placement(origin, axis, 0.0)
    r0 = _geometry(helix, trial, nucleic_name, motif_name).exit(unit, entry.forward)
    r0 = r0 - point
    phase = math.atan2(
        float(np.dot(np.cross(r0, radial), axis)), float(np.dot(r0, radial))
    )

    def gap(candidate: float) -> float:
        geom = _geometry(
            helix, _Placement(origin, axis, candidate), nucleic_name, motif_name
        )
        return float(np.linalg.norm(geom.exit(unit, entry.forward) - target))

    best = min((phase, -phase), key=gap)
    return _Placement(origin, axis, best)


def _place(
    entries: list[_Entry], helices: list[_Helix], nucleic_name: str, motif_name: str
) -> tuple[list[_Placement], int, int]:
    """A nominal placement per helix, plus how many 0-nt crossings were
    realised end to end and how many were left as crossovers.

    The first helix sits at the origin along ``+z``. Then every 0-nt
    crossing, in strand order: the unplaced side is stacked on the placed
    one (:func:`_stacked`, either direction — a helix placed earlier is
    the anchor); when neither is placed the earlier helix takes the next
    free slot first. A crossing whose two helices are BOTH already placed
    is left as a crossover and counted — a helix end stacks on one
    neighbour, and the fold asked for a second, which is a strained
    junction ``relax_chain`` and ``chain_loop_short`` will show rather than
    a placement this pass can fake. Every other helix takes the next free
    slot, one :data:`precis_se.chain.nucleic.HELIX_SPACING_M` along ``+x``,
    in the order the strand reaches it.
    """
    total, _coaxial = _junctions(entries)
    placed: list[_Placement | None] = [None] * len(helices)
    slot = 0

    def side_by_side() -> _Placement:
        nonlocal slot
        out = _Placement(
            np.array([slot * nucleic.HELIX_SPACING_M, 0.0, 0.0]),
            np.array([0.0, 0.0, 1.0]),
            0.0,
        )
        slot += 1
        return out

    placed[entries[0].helix] = side_by_side()
    stacked = strained = 0
    for before, after in itertools.pairwise(entries):
        if before.helix == after.helix or after.start - before.end - 1 != 0:
            continue
        extra = total[frozenset((before.helix, after.helix))]
        a, b = placed[before.helix], placed[after.helix]
        if a is None and b is None:
            a = placed[before.helix] = side_by_side()
        if b is None:
            assert a is not None
            placed[after.helix] = _stacked(
                a,
                helices[before.helix],
                before,
                helices[after.helix],
                after,
                extra,
                anchor_leaves=True,
                nucleic_name=nucleic_name,
                motif_name=motif_name,
            )
            stacked += 1
        elif a is None:
            placed[before.helix] = _stacked(
                b,
                helices[after.helix],
                after,
                helices[before.helix],
                before,
                extra,
                anchor_leaves=False,
                nucleic_name=nucleic_name,
                motif_name=motif_name,
            )
            stacked += 1
        else:
            strained += 1
    for entry in entries:
        if placed[entry.helix] is None:
            placed[entry.helix] = side_by_side()
    out: list[_Placement] = []
    for placement in placed:
        assert placement is not None  # every helix has an entry, so every one is placed
        out.append(placement)
    return out, stacked, strained


def _strand_sequence(node: Any) -> str | None:
    record = node.chain or {}
    value = record.get("sequence")
    return str(value) if value else None


def _sequenced_strands(tree: Any) -> dict[str, tuple[str, str]]:
    """``{strand block: (sequence, nucleic)}`` for every strand that
    carries one. A design with none is not a fold-checkable design at all,
    which is what keeps these findings off every other ``se`` design."""
    out: dict[str, tuple[str, str]] = {}
    for name in sorted(getattr(tree, "blocks", {})):
        node = tree.blocks[name]
        if chain_role(node) != STRAND_ROLE:
            continue
        sequence = _strand_sequence(node)
        if sequence is None:
            continue
        record = node.chain or {}
        out[name] = (sequence, str(record.get("nucleic") or "DNA"))
    return out


def sequence_positions(route: list[DomainSpec]) -> dict[tuple[int, int], int]:
    """``{(domain ord, helix offset): sequence position}`` for one route.

    The same 5'→3' accounting
    :func:`precis_se.chain.pairing.strand_letters` does — every domain's
    offsets in traversal order with each ``loop_before_nt`` consuming its
    own positions in between — kept as positions rather than letters
    because both fold checks compare *indices* (what pairs with what),
    not bases.
    """
    out: dict[tuple[int, int], int] = {}
    cursor = 0
    for domain in sorted(route, key=lambda d: d.ord):
        cursor += domain.loop_before_nt or 0
        for offset in domain.offsets():
            out[(domain.ord, offset)] = cursor
            cursor += 1
    return out


# ── the op ──────────────────────────────────────────────────────────────


def op_fold_layout(tree: SeTree, op: dict[str, Any]) -> str:
    """Apply ``fold_layout`` and return its summary line.

    ``strand=`` names the strand block (minted when it does not exist yet,
    under ``parent=``), ``sequence=`` its letters (or the block's own
    ``declare_strand`` sequence when it already has one) and ``nucleic=``
    ``'DNA'``/``'RNA'``. One helix block ``<strand>.h<k>`` per stack of the
    MFE structure, two antiparallel domains on each, the unpaired
    stretches between consecutive domains as ``loop_before_nt``, and one
    single-occupancy stub helix ``<strand>.t5``/``.t3`` per unpaired tail
    carrying the strand's first/last domain.

    **Shapes covered**: every pseudoknot-free structure — a hairpin, a
    multiloop/multi-branch fold and any nesting of those; a bulge, a
    one-sided internal loop or two coaxially stacked helices (ZERO unpaired
    nucleotides between two domains: the two helices are placed end to end,
    :func:`_stacked`, with a ``loop_before_nt`` of 0 on that crossing — a
    helix end stacks on one neighbour, so a further 0-nt crossing onto an
    end already taken is left as a crossover and named in the summary);
    and an unpaired 5'/3' tail (a stub helix, reached the same way).

    **Shapes refused, by name** (a wrong layout is worse than no layout):
    a fold with no pairs at all; a pseudoknotted dot-bracket
    (:func:`pair_table`), which ViennaRNA's MFE never produces; a strand
    that already routes domains; and > :data:`MAX_LAYOUT_NT` nt.

    Raises :class:`~precis.errors.Unsupported` when the ``[chain]`` extra
    is absent, and :class:`~precis_se.ops.OpError` before any mutation on
    every other refusal path — the op lands whole or not at all.
    """
    strays = sorted(set(op) - _ALLOWED_KEYS)
    if strays:
        raise OpError(
            f"fold_layout: unknown key(s) {', '.join(strays)} — takes strand "
            "(the strand block; minted if absent), sequence, nucleic "
            "('DNA'|'RNA') and parent"
        )
    name = str(op.get("strand") or "").strip()
    if not name:
        raise OpError(
            "fold_layout needs strand='<block name>' — the strand whose "
            "sequence is folded (it is minted, with its helices, when the "
            "block does not exist yet)"
        )
    node = tree.blocks.get(name)
    parent = op.get("parent")
    record = (node.chain if node is not None else None) or {}
    if node is not None and record and record.get("role") != STRAND_ROLE:
        raise OpError(
            f"fold_layout: block {name!r} is a {record.get('role')!r}, not a "
            "strand — fold_layout writes a strand's route, and a helix's "
            "geometry is declare_helix's"
        )
    existing = [d for d in getattr(tree, "domains", []) if d.strand == name]
    if existing:
        raise OpError(
            f"fold_layout: strand {name!r} already routes {len(existing)} "
            "domain(s) — clear_chain the strand (or remove_domain them) "
            "first; folding on top would double-route it"
        )
    raw = op.get("sequence") or record.get("sequence")
    try:
        nucleic_name = chain_vocab.resolve_nucleic(
            op.get("nucleic") or record.get("nucleic"), "fold_layout"
        )
        sequence = chain_vocab.vet_sequence(raw, nucleic_name, "fold_layout")
    except ChainError as exc:
        raise OpError(str(exc)) from exc
    if not sequence:
        raise OpError(
            f"fold_layout: strand {name!r} has no sequence — pass "
            "sequence='ACGT…' (or declare_strand it first); there is nothing "
            "to fold without letters"
        )
    if len(sequence) > MAX_LAYOUT_NT:
        raise OpError(
            f"fold_layout: {len(sequence)} nt is past the {MAX_LAYOUT_NT} nt "
            "bound — RNA.fold is O(n³), so this would run for many minutes; "
            "fold the domains you are designing, not the whole construct"
        )
    rna = rna_module()
    if rna is None:
        raise Unsupported(
            "fold_layout needs ViennaRNA, which ships in the optional "
            f"[chain] extra ({INSTALL_HINT}) — no fold is better than a "
            "guessed one",
            next=INSTALL_HINT,
        )
    structure, energy = mfe_fold(rna, sequence)
    try:
        stack_list = stacks(pair_table(structure))
    except ChainError as exc:
        raise OpError(f"fold_layout: {exc}") from exc
    if not stack_list:
        raise OpError(
            f"fold_layout: the MFE structure of these {len(sequence)} nt is "
            f"entirely unpaired ({structure}) — there is no helix to lay out "
            f"({RNA_PARAMS_NOTE})"
        )
    helices, entries = _helices_and_entries(stack_list, len(sequence))
    loops = [
        after.start - before.end - 1 for before, after in itertools.pairwise(entries)
    ]
    motif_name = nucleic.MOTIF_FOR_NUCLEIC[nucleic_name]
    helix_names = [f"{name}.{h.label}" for h in helices]
    clashes = sorted(h for h in helix_names if h in tree.blocks)
    if clashes:
        raise OpError(
            f"fold_layout: block(s) {', '.join(clashes)} already exist — "
            "this op mints one helix per stack under those names; remove "
            "them, or fold under a different strand name"
        )
    if node is None and parent is not None and str(parent) not in tree.blocks:
        raise OpError(f"fold_layout: parent block {parent!r} is not in the tree")
    ops: list[dict[str, Any]] = []
    if node is None:
        mint: dict[str, Any] = {"op": "add_block", "name": name}
        if parent is not None:
            mint["parent"] = str(parent)
        ops.append(mint)
    ops.append(
        {
            "op": "declare_strand",
            "block": name,
            "sequence": sequence,
            "nucleic": nucleic_name,
        }
    )
    placements, n_stacked, n_strained = _place(
        entries, helices, nucleic_name, motif_name
    )
    rise = nucleic.MOTIFS[motif_name].rise
    for helix_name, helix, placement in zip(
        helix_names, helices, placements, strict=True
    ):
        mint = {"op": "add_block", "name": helix_name}
        if node is None and parent is not None:
            mint["parent"] = str(parent)
        elif node is not None and node.parent:
            mint["parent"] = str(node.parent)
        ops.append(mint)
        declare: dict[str, Any] = {
            "op": "declare_helix",
            "block": helix_name,
            "n_units": helix.n_units,
            "nucleic": nucleic_name,
            "path": _path(placement, helix.n_units, rise),
        }
        if placement.phase0:
            declare["phase0"] = f"{placement.phase0} rad"
        ops.append(declare)
    for i, entry in enumerate(entries):
        domain: dict[str, Any] = {
            "op": "add_domain",
            "strand": name,
            "helix": helix_names[entry.helix],
            "start": 0,
            "end": helices[entry.helix].n_units,
            "forward": entry.forward,
        }
        if helices[entry.helix].paired:
            # Every pair an MFE fold makes is cis Watson-Crick — the family
            # that also carries the G·U wobble — so the derived pairing can
            # state a family instead of leaving it undeclared. A tail stub
            # pairs with nothing and states none.
            domain["geometry"] = "W-W-cis"
        if i:
            domain["loop_before_nt"] = loops[i - 1]
        ops.append(domain)
    apply_ops(tree, ops)
    n_bp = sum(len(s) for s in stack_list)
    tails = [h for h in helices if not h.paired]
    summary = f"{len(stack_list)} " + ("helix" if len(stack_list) == 1 else "helices")
    summary += f" ({n_bp} bp)"
    if tails:
        summary += " + " + ", ".join(
            f"{'5' if h.label == 't5' else '3'}' tail of {h.n_units} nt as "
            f"single-occupancy stub {name}.{h.label}"
            for h in tails
        )
    return (
        f"fold_layout: folded {len(sequence)} nt of {nucleic_name} to "
        f"{structure} at {energy:.2f} kcal/mol ({RNA_PARAMS_NOTE}); wrote "
        f"{summary}, strand {name!r} and "
        f"{len(entries)} domain(s) with {len(loops)} loop(s) "
        f"({', '.join(f'{n} nt' for n in loops) or 'none'})"
        + (f", {n_stacked} coaxial junction(s) placed end to end" if n_stacked else "")
        + (
            f", {n_strained} 0-nt crossing(s) left as a crossover (its helix end "
            "already stacks on another; relax_chain and chain_loop_short will show it)"
            if n_strained
            else ""
        )
        + " — placement is NOMINAL (straight helices "
        f"{format_quantity(nucleic.HELIX_SPACING_M, 'length')} apart, coaxial "
        "ones end to end); run layout_chain then relax_chain to settle it"
    )


# ── the findings ────────────────────────────────────────────────────────


def _declared_self_pairs(
    route: list[DomainSpec], positions: dict[tuple[int, int], int]
) -> set[tuple[int, int]]:
    """Which sequence positions of ONE strand the route declares paired
    with each other — two of its own domains on the same helix offset.

    Only self-pairing: a staple paired to a scaffold is a fact about two
    strands and an isolated MFE fold cannot see it, so comparing it would
    report every staple in a correct origami.
    """
    by_offset: dict[tuple[str, int], list[int]] = {}
    for domain in route:
        for offset in domain.offsets():
            pos = positions.get((domain.ord, offset))
            if pos is not None:
                by_offset.setdefault((domain.helix, offset), []).append(pos)
    out: set[tuple[int, int]] = set()
    for hits in by_offset.values():
        if len(hits) == 2:
            a, b = sorted(hits)
            out.add((a, b))
    return out


def _disagree_row(
    strand: str,
    structure: str,
    energy: float,
    declared: set[tuple[int, int]],
    folded: set[tuple[int, int]],
) -> ValidationIssue | None:
    """``chain_fold_disagree`` for one strand, or ``None`` when the route's
    own self-pairing IS the MFE fold."""
    missing = sorted(declared - folded)
    extra = sorted(folded - declared)
    if not missing and not extra:
        return None

    def sample(pairs: list[tuple[int, int]]) -> str:
        shown = ", ".join(f"{a}·{b}" for a, b in pairs[:4])
        return shown + (f", +{len(pairs) - 4} more" if len(pairs) > 4 else "")

    parts = []
    if missing:
        parts.append(
            f"{len(missing)} declared pair(s) the fold does not make "
            f"({sample(missing)})"
        )
    if extra:
        parts.append(
            f"{len(extra)} pair(s) the fold makes that the route does not "
            f"declare ({sample(extra)})"
        )
    return ValidationIssue(
        rule="chain_fold_disagree",
        subject=strand,
        detail=(
            f"the route declares {len(declared)} self-pair(s); the MFE fold "
            f"{structure} ({energy:.2f} kcal/mol) makes {len(folded)} — "
            + "; ".join(parts)
            + f". Sequence positions, 0-based, 5'→3'. {RNA_PARAMS_NOTE}"
        ),
        severity="warn",
    )


def _complementary(a: str, b: str) -> bool:
    return _RC.get(a) == b


def _offtarget_runs(
    seqs: dict[str, str], intended: dict[tuple[str, int], tuple[str, int]]
) -> list[tuple[int, str, int, str, int]]:
    """Maximal unintended complementary runs as
    ``(length, strand_a, start_a, strand_b, start_b)``, where
    ``a[start_a + t]`` pairs ``b[start_b + length - 1 - t]``.

    A 6-mer hash index over every strand (:data:`OFFTARGET_SEED_NT`) gives
    the seeds — O(total length) to build and one dict lookup per window, so
    no pair of strands is ever aligned. A seed containing any *intended*
    pair is dropped whole, which is what keeps a correct origami's declared
    duplexes (and a ``fold_layout`` design's own stem) out of the report;
    the survivors are extended outward while still complementary and still
    unintended, and overlapping extensions of one duplex collapse to the
    same run.
    """
    k = OFFTARGET_SEED_NT
    rna = {name: as_rna(seq) for name, seq in seqs.items()}
    index: dict[str, list[tuple[str, int]]] = {}
    for name, seq in rna.items():
        for p in range(len(seq) - k + 1):
            window = seq[p : p + k]
            if any(c not in _RC for c in window):
                continue  # an N is a deliberate don't-know, not a match
            index.setdefault(window, []).append((name, p))
    runs: dict[tuple[str, int, str, int], int] = {}
    for a_name, a_seq in rna.items():
        for i in range(len(a_seq) - k + 1):
            window = a_seq[i : i + k]
            if any(c not in _RC for c in window):
                continue
            target = "".join(_RC[c] for c in reversed(window))
            for b_name, j in index.get(target, ()):
                if a_name == b_name and abs(i - j) < k:
                    continue  # the same stretch read against itself
                if (a_name, i, b_name, j) > (b_name, j, a_name, i):
                    continue  # each duplex is found from both sides
                if any(
                    intended.get((a_name, i + t)) == (b_name, j + k - 1 - t)
                    for t in range(k)
                ):
                    continue  # this IS the declared duplex
                start_a, start_b, length = i, j, k
                b_seq = rna[b_name]
                while (
                    start_a > 0
                    and start_b + length < len(b_seq)
                    and _complementary(a_seq[start_a - 1], b_seq[start_b + length])
                    and intended.get((a_name, start_a - 1))
                    != (b_name, start_b + length)
                ):
                    start_a -= 1
                    length += 1
                while (
                    start_a + length < len(a_seq)
                    and start_b > 0
                    and _complementary(a_seq[start_a + length], b_seq[start_b - 1])
                    and intended.get((a_name, start_a + length))
                    != (b_name, start_b - 1)
                ):
                    start_b -= 1
                    length += 1
                if a_name == b_name and start_b < start_a + length:
                    continue  # a self-run that has grown into itself
                key = (a_name, start_a, b_name, start_b)
                runs[key] = max(runs.get(key, 0), length)
    return sorted(
        (
            (length, a_name, start_a, b_name, start_b)
            for (a_name, start_a, b_name, start_b), length in runs.items()
            if length >= OFFTARGET_MIN_NT
        ),
        key=lambda r: (-r[0], r[1], r[2], r[3], r[4]),
    )


def _intended_pairs(tree: Any) -> dict[tuple[str, int], tuple[str, int]]:
    """``{(strand, sequence position): (strand, position)}`` for every
    derived base pair — what the design MEANT to pair, so the off-target
    scan can subtract it. Built from
    :func:`precis_se.chain.pairing.derive_pairing`, so "intended" means
    exactly what the rest of the chain tier means by paired."""
    pairing = derive_pairing(tree)
    positions: dict[str, dict[tuple[int, int], int]] = {}
    for domain in getattr(tree, "domains", []) or []:
        positions.setdefault(domain.strand, {})
    for strand in positions:
        route = [d for d in tree.domains if d.strand == strand]
        positions[strand] = sequence_positions(route)
    out: dict[tuple[str, int], tuple[str, int]] = {}
    for occ in pairing.offsets.values():
        if occ.status != PAIRED:
            continue
        seats = [
            (o.strand, positions.get(o.strand, {}).get((o.ord, occ.offset)))
            for o in occ.occupants
        ]
        if any(pos is None for _strand, pos in seats):
            continue
        (sa, pa), (sb, pb) = seats
        assert pa is not None and pb is not None
        out[(sa, pa)] = (sb, pb)
        out[(sb, pb)] = (sa, pa)
    return out


def findings(tree: Any) -> list[ValidationIssue]:
    """The three fold findings for ``tree`` — append-only, and empty for
    any design with no sequenced strand (which is every non-chain ``se``
    design, and a chain design before its sequence exists).

    ``chain_fold_unavailable`` (info) is **one row for the design**, not one
    per strand: the library is absent or present, and N copies of one fact
    is a worse report. The off-target scan runs either way — it is a hash
    index over the stored letters and needs no library.

    ``chain_fold_skipped`` (info) is the eleventh-plus rule this item adds
    beyond the nine the spec listed, for the same reason ``chain_malformed``
    is: the alternative is a 6 kb scaffold that reads as fold-checked
    because nothing said otherwise. It names the length and the bound.
    """
    seqs = _sequenced_strands(tree)
    if not seqs:
        return []
    rows: list[ValidationIssue] = []
    rna = rna_module()
    if rna is None:
        rows.append(
            ValidationIssue(
                rule="chain_fold_unavailable",
                subject=f"{len(seqs)} sequenced strand(s)",
                detail=(
                    "ViennaRNA is not installed, so no fold check ran "
                    "(chain_fold_disagree). It ships in the optional [chain] "
                    f"extra: {INSTALL_HINT}. The off-target scan below needs "
                    "no library and DID run"
                ),
                severity="info",
            )
        )
    else:
        routes: dict[str, list[DomainSpec]] = {}
        for domain in getattr(tree, "domains", []) or []:
            routes.setdefault(domain.strand, []).append(domain)
        for strand, (sequence, _nucleic) in seqs.items():
            route = routes.get(strand)
            if not route:
                continue  # nothing declared to disagree with
            if len(sequence) > MAX_CHECK_NT:
                rows.append(
                    ValidationIssue(
                        rule="chain_fold_skipped",
                        subject=strand,
                        detail=(
                            f"{len(sequence)} nt is past the {MAX_CHECK_NT} nt "
                            "fold-check bound (RNA.fold is O(n³)) — this "
                            "strand was NOT folded, so no "
                            "chain_fold_disagree can exist for it. Fold it "
                            "deliberately with fold_layout, which takes "
                            "scaffold length"
                        ),
                        severity="info",
                    )
                )
                continue
            structure, energy = mfe_fold(rna, sequence)
            positions = sequence_positions(route)
            declared = _declared_self_pairs(route, positions)
            folded = {(i, j) for i, j in pair_table(structure).items() if i < j}
            row = _disagree_row(strand, structure, energy, declared, folded)
            if row is not None:
                rows.append(row)
    intended = _intended_pairs(tree)
    runs = _offtarget_runs({n: s for n, (s, _nuc) in seqs.items()}, intended)
    for length, a_name, start_a, b_name, start_b in runs[:MAX_OFFTARGET_ROWS]:
        window = seqs[a_name][0][start_a : start_a + length]
        rows.append(
            ValidationIssue(
                rule="chain_offtarget",
                subject=(
                    f"{a_name}[{start_a}:{start_a + length}] ↔ "
                    f"{b_name}[{start_b}:{start_b + length}]"
                ),
                detail=(
                    f"{length} nt of unintended complementarity ({window}) "
                    "between two stretches the design does not route as a "
                    "pair — sequence positions, 0-based. Redesign one "
                    f"stretch, or accept it (≥ {OFFTARGET_MIN_NT} nt is the "
                    "reporting threshold, found on a 6-mer index)"
                ),
                severity="warn",
            )
        )
    if len(runs) > MAX_OFFTARGET_ROWS:
        rows.append(
            ValidationIssue(
                rule="chain_offtarget",
                subject=f"+{len(runs) - MAX_OFFTARGET_ROWS} more",
                detail=(
                    f"{len(runs)} unintended complementary runs of "
                    f"≥ {OFFTARGET_MIN_NT} nt in total; the "
                    f"{MAX_OFFTARGET_ROWS} longest are above. Chance "
                    "8-mers are common in a long design — treat the count as "
                    "the figure and the longest runs as the candidates"
                ),
                severity="warn",
            )
        )
    return rows
