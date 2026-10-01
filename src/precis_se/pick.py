"""Pick anything, get every level it belongs to — the reference grammar.

The resolver half of ``docs/backlog/se-pick-hierarchy.md`` (gap 2), built
for the case whose data already exists: an atom of a ``realize_chain``
structure. One atom ordinal becomes a list of :class:`PickLevel` rows,
innermost first — atom, residue, base pair, strand domain, strand, segment
block, helix, ancestors — and every row carries a **token** a prompt can
cite and :func:`resolve_token` can read back. Store-free: the caller hands
in the tree, the bound scene's atom labels and the structure's
``chain_atoms`` record (:func:`precis_se.atomic.render.bound_chain_records`).

**Token grammar.** The key is always a block ``uid``
(:mod:`precis_se.identity`), never a label — a label is display only and a
rename must not break a citation:

- ``<se:UID>`` — a block.
- ``<se:UID#ORD>`` — atom ``ORD`` (0-based, scene order) of the structure
  bound to that block. Stable per version of the bound structure.
- ``<se:UID/REGION>`` — a part of a block. Two regions resolve today:
  ``<chain>.<resseq>`` (a residue of the bound ``realize_chain`` structure,
  e.g. ``<se:41/A.8>``) and ``d<ord>`` on a strand block (that strand's
  ``ord``-th domain, e.g. ``<se:38/d1>``). A hexfold module region is the
  same shape and is not resolved yet.
- ``<se:UID@OFFSET>`` on a **helix** block — helix offset ``OFFSET``, the
  base pair when two antiparallel strands occupy it. The backlog item
  first wrote this ``<se:UID@h0:3>``; that keyed on the helix *label*
  against the grammar's own rule, and a helix is a block with a uid of its
  own. ``@`` means a helix offset and nothing else (Reto, 2026-10-01: one
  glyph, one meaning — it already reads ``stem@3`` in every label); a
  datum token, when one is wanted, gets its own glyph. No base letter in
  the token: the address survives a sequence edit, the row shows letters.

A structure that ``realize_chain`` did not mint still resolves: the atom
row names the scene label and the block rows follow, with no residue
levels between.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from precis_se.chain.pairing import PAIRED, UNPAIRED, derive_pairing
from precis_se.chain.vocab import HELIX_ROLE, STRAND_ROLE, chain_role
from precis_se.identity import block_by_uid


class PickError(ValueError):
    """A token that does not parse, or parses and names nothing in this
    design. The message says which and what would resolve."""


@dataclass(frozen=True)
class PickLevel:
    """One row of a pick: what kind of thing, what to show, what to cite."""

    level: str
    label: str
    token: str


@dataclass(frozen=True)
class PickRef:
    """A parsed token — at most one of ``atom``/``region``/``offset``."""

    uid: int
    atom: int | None = None
    region: str | None = None
    offset: int | None = None


_TOKEN = re.compile(r"^<se:(\d+)(?:#(\d+)|/([^<>#@/\s]+)|@(\d+))?>$")
_RESIDUE_REGION = re.compile(r"^([A-Za-z0-9]+)\.(-?\d+)$")
_DOMAIN_REGION = re.compile(r"^d(\d+)$")


def format_token(
    uid: int,
    *,
    atom: int | None = None,
    region: str | None = None,
    offset: int | None = None,
) -> str:
    """The token text for one reference (module docstring's grammar)."""
    if atom is not None:
        return f"<se:{uid}#{atom}>"
    if region is not None:
        return f"<se:{uid}/{region}>"
    if offset is not None:
        return f"<se:{uid}@{offset}>"
    return f"<se:{uid}>"


def parse_token(text: Any) -> PickRef:
    """``text`` → :class:`PickRef`. Pure syntax: an unknown uid parses and
    fails to resolve later, the same split :func:`precis_se.identity.parse_uid`
    makes."""
    match = _TOKEN.match(str(text).strip())
    if match is None:
        raise PickError(
            f"{text!r} is not an se reference token — the forms are <se:UID>, "
            "<se:UID#ATOM>, <se:UID/REGION> and <se:UID@OFFSET>"
        )
    uid, atom, region, offset = match.groups()
    return PickRef(
        uid=int(uid),
        atom=int(atom) if atom is not None else None,
        region=region,
        offset=int(offset) if offset is not None else None,
    )


def _uid(node: Any) -> int:
    if node.uid is None:
        raise PickError(
            f"block {node.name!r} has no uid yet — a reference token needs a "
            "saved design"
        )
    return int(node.uid)


def _block_levels(tree: Any, node: Any) -> list[PickLevel]:
    """``node`` and its ancestors, innermost first. A chain block is
    levelled by its role (segment/helix/strand), anything else ``block``."""
    out: list[PickLevel] = []
    seen: set[str] = set()
    while node is not None and node.name not in seen:
        seen.add(node.name)
        out.append(
            PickLevel(chain_role(node) or "block", node.name, format_token(_uid(node)))
        )
        node = tree.blocks.get(node.parent) if node.parent else None
    return out


def _residue_rows(record: Mapping[str, Any]) -> dict[tuple[str, int], Sequence[Any]]:
    return {(str(r[0]), int(r[1])): r for r in record.get("residues") or []}


def _resname(record: Mapping[str, Any], chain: str, resseq: int) -> str | None:
    chain_ids = record.get("chain_ids") or []
    seqs = record.get("resseq") or []
    resnames = record.get("resnames") or []
    for c, r, name in zip(chain_ids, seqs, resnames, strict=False):
        if str(c) == chain and int(r) == resseq:
            return str(name)
    return None


def _offset_levels(tree: Any, helix: Any, offset: int) -> list[PickLevel]:
    """The row for one helix offset: ``pair`` when two antiparallel strands
    occupy it (marked unpaired or not — the label says which), ``offset``
    otherwise (single, parallel, crowded or empty), with each occupant's
    letter in occupant order — joined ``·`` only for a pair, ``/`` for
    anything else, since ``G·C`` reads as a base pair."""
    occupancy = derive_pairing(tree).at(helix.name, offset)
    label = f"{helix.name}@{offset}"
    level = "offset"
    if occupancy is None:
        label += " (unoccupied)"
    else:
        joint = "·" if occupancy.status == PAIRED else " / "
        letters = joint.join(o.letter or "?" for o in occupancy.occupants)
        strands = " / ".join(f"{o.strand}.{o.ord}" for o in occupancy.occupants)
        label += f" ({letters}, {occupancy.status}: {strands})"
        if occupancy.status in (PAIRED, UNPAIRED):
            level = "pair"
    return [PickLevel(level, label, format_token(_uid(helix), offset=offset))]


def _domain_levels(tree: Any, strand_name: str, ord_: int) -> list[PickLevel]:
    """The strand domain ``<strand>.<ord>`` and its strand block. Empty when
    the strand block is gone (a structure that outlived its route)."""
    strand = tree.blocks.get(strand_name)
    if strand is None:
        return []
    label = f"{strand_name}.{ord_}"
    for domain in getattr(tree, "domains", None) or []:
        if domain.strand == strand_name and int(domain.ord) == ord_:
            arrow = "5'→3'" if domain.forward else "3'←5'"
            label += f" ({domain.helix}[{domain.start}, {domain.end}) {arrow})"
            break
    return [
        PickLevel("domain", label, format_token(_uid(strand), region=f"d{ord_}")),
        PickLevel("strand", strand.name, format_token(_uid(strand))),
    ]


_NO_RESIDUE_ROWS = (
    "realized before residue rows were stored — re-realize the segment "
    "for its base pair and domain"
)


def _bare_residue_level(
    node: Any, record: Mapping[str, Any], chain: str, resseq: int
) -> PickLevel | None:
    """The residue row of a ``chain_atoms`` record that predates its
    ``residues`` list: the residue is named from the atom columns, and the
    label says why no pair or domain row follows instead of leaving them
    silently out. ``None`` when the record has residue rows (its own
    lookup answers) or no such residue."""
    if record.get("residues"):
        return None
    name = _resname(record, chain, resseq)
    if name is None:
        return None
    return PickLevel(
        "residue",
        f"{name} {resseq} (chain {chain}; {_NO_RESIDUE_ROWS})",
        format_token(_uid(node), region=f"{chain}.{resseq}"),
    )


def _residue_levels(
    tree: Any, node: Any, record: Mapping[str, Any], row: Sequence[Any]
) -> list[PickLevel]:
    """Residue → base pair → domain → strand for one ``chain_atoms.residues``
    row (``[chain id, resseq, strand, ord, offset | None, letter,
    insertion index]``). A loop nucleotide (offset ``None``) sits on no
    helix offset and in no domain — its row's ``ord`` is the domain the
    loop *leaves* — so it has neither a pair row nor a domain row, and its
    residue label says which loop. An inserted base (index ``i > 0``) is
    named ``<helix>@<offset>+<i>``: it is in its domain but not in the
    offset's pair, so it gets the domain rows and no pair row."""
    chain, resseq, strand, ord_, offset, letter = (
        str(row[0]),
        int(row[1]),
        str(row[2]),
        int(row[3]),
        row[4],
        row[5],
    )
    ins = int(row[6] or 0) if len(row) > 6 else 0
    name = _resname(record, chain, resseq) or (str(letter) if letter else "nt")
    if offset is None:
        where = f"loop nucleotide after {strand}.{ord_}"
    elif ins:
        where = (
            f"chain {chain}; inserted base {record.get('helix')}@{offset}+{ins}, "
            "a bulge off the duplex"
        )
    else:
        where = f"chain {chain}"
    out = [
        PickLevel(
            "residue",
            f"{name} {resseq} ({where})",
            format_token(_uid(node), region=f"{chain}.{resseq}"),
        )
    ]
    helix = tree.blocks.get(str(record.get("helix") or ""))
    if offset is not None and not ins and helix is not None:
        out += _offset_levels(tree, helix, int(offset))
    levels = _domain_levels(tree, strand, ord_)
    return out + (levels[1:] if offset is None else levels)


def atom_levels(
    tree: Any,
    node: Any,
    ordinal: int,
    *,
    labels: Sequence[str],
    record: Mapping[str, Any] | None = None,
) -> list[PickLevel]:
    """Every level atom ``ordinal`` of the structure bound to ``node``
    belongs to, innermost first. ``labels`` are the bound scene's atom
    labels in scene order; ``record`` is the structure's ``chain_atoms``
    record when ``realize_chain`` minted it. A record whose columns do not
    match the scene (a structure edited after it was realized) is ignored
    rather than trusted — the rows then stop at atom → block."""
    if not 0 <= ordinal < len(labels):
        raise PickError(
            f"atom {ordinal} is outside the structure bound to {node.name!r} — "
            f"it has {len(labels)} atom(s), ordinals 0..{len(labels) - 1}"
        )
    uid = _uid(node)
    token = format_token(uid, atom=ordinal)
    columns = [
        (record or {}).get(k) or []
        for k in ("names", "resnames", "resseq", "chain_ids")
    ]
    if not record or any(len(col) != len(labels) for col in columns):
        return [
            PickLevel("atom", str(labels[ordinal]), token),
            *_block_levels(tree, node),
        ]
    names, resnames, resseq, chain_ids = columns
    out = [
        PickLevel(
            "atom", f"{names[ordinal]} of {resnames[ordinal]} {resseq[ordinal]}", token
        )
    ]
    chain, seq = str(chain_ids[ordinal]), int(resseq[ordinal])
    row = _residue_rows(record).get((chain, seq))
    if row is not None:
        out += _residue_levels(tree, node, record, row)
    elif (bare := _bare_residue_level(node, record, chain, seq)) is not None:
        out.append(bare)
    return out + _block_levels(tree, node)


def atom_hover_names(
    labels: Sequence[str], record: Mapping[str, Any] | None = None
) -> list[str]:
    """One short name per atom, in scene order, for the 3D viewer's hover
    readout: ``O3' · DG 4 (A)`` when ``record`` (a ``realize_chain``
    ``chain_atoms`` record) has columns aligned with ``labels``, the scene
    label otherwise — the same fallback :func:`atom_levels` takes."""
    columns = [
        (record or {}).get(k) or []
        for k in ("names", "resnames", "resseq", "chain_ids")
    ]
    if not record or any(len(col) != len(labels) for col in columns):
        return [str(label) for label in labels]
    names, resnames, resseq, chain_ids = columns
    return [
        f"{names[i]} · {resnames[i]} {resseq[i]} ({chain_ids[i]})"
        for i in range(len(labels))
    ]


def resolve_token(
    tree: Any,
    ref: PickRef,
    *,
    labels: Sequence[str] | None = None,
    record: Mapping[str, Any] | None = None,
) -> list[PickLevel]:
    """A parsed token → the row it names, followed by every level above it.

    ``labels``/``record`` describe the structure bound to the token's block
    (see :func:`atom_levels`); the caller looks them up for
    ``block_by_uid(tree, ref.uid)`` and passes ``None`` for an unbound
    block."""
    node = block_by_uid(tree, ref.uid)
    if node is None:
        raise PickError(
            f"no block with uid {ref.uid} in this design — a block's uid is on "
            "view='block' (args={'name': <label>})"
        )
    if ref.atom is not None:
        if labels is None:
            raise PickError(
                f"block {node.name!r} has no bound structure, so "
                f"{format_token(ref.uid, atom=ref.atom)} names no atom"
            )
        return atom_levels(tree, node, ref.atom, labels=labels, record=record)
    if ref.region is not None:
        domain = _DOMAIN_REGION.match(ref.region)
        if domain is not None and chain_role(node) == STRAND_ROLE:
            ord_ = int(domain.group(1))
            if not any(
                d.strand == node.name and int(d.ord) == ord_
                for d in getattr(tree, "domains", None) or []
            ):
                raise PickError(f"strand {node.name!r} has no domain {ord_}")
            return _domain_levels(tree, node.name, ord_)
        residue = _RESIDUE_REGION.match(ref.region)
        if residue is not None and record:
            key = (residue.group(1), int(residue.group(2)))
            row = _residue_rows(record).get(key)
            bare = _bare_residue_level(node, record, *key)
            if row is None and bare is not None:
                return [bare, *_block_levels(tree, node)]
            if row is None:
                raise PickError(
                    f"the structure bound to {node.name!r} has no residue "
                    f"{key[0]}.{key[1]}"
                )
            return _residue_levels(tree, node, record, row) + _block_levels(tree, node)
        raise PickError(
            f"region {ref.region!r} of block {node.name!r} does not resolve — "
            "the regions that do are '<chain>.<resseq>' on a realize_chain "
            "segment and 'd<ord>' on a strand"
        )
    if ref.offset is not None:
        if chain_role(node) == HELIX_ROLE:
            return _offset_levels(tree, node, ref.offset) + _block_levels(tree, node)
        raise PickError(
            f"'@{ref.offset}' names a helix offset, and block {node.name!r} is "
            "not a helix"
        )
    return _block_levels(tree, node)
