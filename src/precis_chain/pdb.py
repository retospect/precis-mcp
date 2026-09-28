"""PDB and mmCIF atom records: write coordinates out, read a single-atom trace
back in.

The kernel owns this rather than the structure layer for one reason: a chain
importer needs the *reader* with no scene, no store and no element tables — a
protein Calpha trace is ``(n, 3)`` per chain and nothing more. The binding
wraps this with a ten-line adapter over its own scene type
(``docs/backlog/precis-chain-kernel.md``, decisions log 2026-09-27).

**Angstroms, here only.** Every other module in this package is unit-agnostic;
the PDB format is not — its coordinate field is defined in angstroms, so
``coords_A`` is named for what it must contain and the caller converts at this
boundary.

Fidelity: the PDB coordinate field is ``%8.3f``, so a write/read round trip is
exact to 0.001 A and no better. mmCIF is written at the same three decimals on
purpose — two formats that disagree on precision would make an export
comparison meaningless.

Neither writer emits connectivity, and :func:`read_trace` ignores it. Bonds in
a nucleic-acid or protein model are derivable from residue identity and the
backbone order, which is the binding's knowledge, not this module's.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

#: PDB fixed-width field limits. Past either, the format cannot represent the
#: model and the caller is pointed at mmCIF rather than silently handed a file
#: with wrapped or truncated numbering.
_MAX_SERIAL = 99999
_MAX_RESSEQ = 9999


def _check_lengths(
    elements: Sequence[str],
    coords_A: np.ndarray,
    names: Sequence[str],
    resnames: Sequence[str],
    resseq: Sequence[int],
    chain_ids: Sequence[str],
) -> np.ndarray:
    coords = np.asarray(coords_A, dtype=float)
    if coords.ndim != 2 or coords.shape[1] != 3:
        raise ValueError(f"coords_A must be (n, 3), got {coords.shape}")
    n = coords.shape[0]
    for label, seq in (
        ("elements", elements),
        ("names", names),
        ("resnames", resnames),
        ("resseq", resseq),
        ("chain_ids", chain_ids),
    ):
        if len(seq) != n:
            raise ValueError(
                f"{label} has {len(seq)} entries but coords_A has {n} rows"
            )
    if not np.all(np.isfinite(coords)):
        raise ValueError("coords_A must be finite")
    return coords


def _pdb_atom_name(name: str) -> str:
    """The 4-character atom-name field.

    Names of 1-3 characters are indented by one space (so they start in column
    14), which is the PDB convention that keeps a one-letter element symbol in
    column 14 and a two-letter one in column 13. A 4-character name fills the
    field. Longer is refused: truncating would silently rename the atom.
    """
    text = name.strip()
    if len(text) > 4:
        raise ValueError(f"atom name {name!r} exceeds the PDB 4-character field")
    return text.ljust(4) if len(text) == 4 else f" {text.ljust(3)}"


def write_pdb(
    elements: Sequence[str],
    coords_A: np.ndarray,
    names: Sequence[str],
    resnames: Sequence[str],
    resseq: Sequence[int],
    chain_ids: Sequence[str],
    *,
    occupancy: float = 1.0,
    b_factor: float = 0.0,
) -> str:
    """Serialise atoms as PDB ``ATOM`` records plus a trailing ``END``.

    Every sequence argument is per-atom and must be the same length as
    ``coords_A`` ``(n, 3)`` angstroms. ``TER`` records are emitted at each
    change of ``chain_ids``, so a multi-chain model reads back as separate
    chains in any viewer.

    Atom serial numbers are assigned here (1-based, in input order) — a caller
    does not get to choose them, because the only thing they have to be is
    unique and increasing.
    """
    coords = _check_lengths(elements, coords_A, names, resnames, resseq, chain_ids)
    n = coords.shape[0]
    if n > _MAX_SERIAL:
        raise ValueError(
            f"{n} atoms exceeds the PDB serial field ({_MAX_SERIAL}) — "
            "use write_mmcif_atom_site for a model this large"
        )
    lines: list[str] = []
    serial = 0
    for i in range(n):
        seq = int(resseq[i])
        if not 0 <= seq <= _MAX_RESSEQ:
            raise ValueError(
                f"residue number {seq} outside the PDB resSeq field "
                f"[0, {_MAX_RESSEQ}] — use write_mmcif_atom_site instead"
            )
        chain = str(chain_ids[i])[:1] or " "
        serial += 1
        lines.append(
            f"ATOM  {serial:>5d} {_pdb_atom_name(str(names[i]))} "
            f"{str(resnames[i]).strip()[:3]:>3s} {chain}{seq:>4d}    "
            f"{coords[i, 0]:8.3f}{coords[i, 1]:8.3f}{coords[i, 2]:8.3f}"
            f"{occupancy:6.2f}{b_factor:6.2f}          "
            f"{str(elements[i]).strip()[:2]:>2s}"
        )
        last_of_chain = i == n - 1 or str(chain_ids[i + 1]) != str(chain_ids[i])
        if last_of_chain:
            serial += 1
            lines.append(
                f"TER   {serial:>5d}      "
                f"{str(resnames[i]).strip()[:3]:>3s} {chain}{seq:>4d}"
            )
    lines.append("END")
    return "\n".join(lines) + "\n"


def _cif_value(text: str) -> str:
    """One mmCIF data value: quoted when it would otherwise be ambiguous,
    ``.`` when empty. Atom names like ``C1'`` need no quoting — an apostrophe
    only opens a quoted string in the first position."""
    value = str(text).strip()
    if not value:
        return "."
    if any(ch.isspace() for ch in value) or value[0] in "'\"_#$[];":
        return '"' + value.replace('"', "'") + '"'
    return value


def write_mmcif_atom_site(
    elements: Sequence[str],
    coords_A: np.ndarray,
    names: Sequence[str],
    resnames: Sequence[str],
    resseq: Sequence[int],
    chain_ids: Sequence[str],
    *,
    occupancy: float = 1.0,
    b_factor: float = 0.0,
    data_block: str = "precis_chain",
) -> str:
    """Serialise the same atoms as an mmCIF ``_atom_site`` loop.

    A minimal but valid single-block CIF: the twelve ``_atom_site`` tags a
    coordinate consumer needs, no entity/chem_comp hierarchy. It exists for the
    models PDB cannot number (over 99 999 atoms, over 9 999 residues per chain)
    and for tools that only read mmCIF; :func:`read_trace` reads either format
    without being told which.
    """
    coords = _check_lengths(elements, coords_A, names, resnames, resseq, chain_ids)
    tags = [
        "group_PDB",
        "id",
        "type_symbol",
        "label_atom_id",
        "label_comp_id",
        "label_asym_id",
        "label_seq_id",
        "Cartn_x",
        "Cartn_y",
        "Cartn_z",
        "occupancy",
        "B_iso_or_equiv",
    ]
    lines = [f"data_{data_block}", "#", "loop_"]
    lines += [f"_atom_site.{tag}" for tag in tags]
    for i in range(coords.shape[0]):
        lines.append(
            " ".join(
                [
                    "ATOM",
                    str(i + 1),
                    _cif_value(str(elements[i])),
                    _cif_value(str(names[i])),
                    _cif_value(str(resnames[i])),
                    _cif_value(str(chain_ids[i])),
                    str(int(resseq[i])),
                    f"{coords[i, 0]:.3f}",
                    f"{coords[i, 1]:.3f}",
                    f"{coords[i, 2]:.3f}",
                    f"{occupancy:.2f}",
                    f"{b_factor:.2f}",
                ]
            )
        )
    lines.append("#")
    return "\n".join(lines) + "\n"


def _split_cif_row(line: str) -> list[str]:
    """Split one mmCIF loop row into values, honouring single/double quoting."""
    out: list[str] = []
    i = 0
    while i < len(line):
        if line[i].isspace():
            i += 1
            continue
        if line[i] in "'\"":
            quote = line[i]
            i += 1
            start = i
            while i < len(line) and not (
                line[i] == quote and (i + 1 >= len(line) or line[i + 1].isspace())
            ):
                i += 1
            out.append(line[start:i])
            i += 1
        else:
            start = i
            while i < len(line) and not line[i].isspace():
                i += 1
            out.append(line[start:i])
    return out


def _read_trace_cif(text: str, atom_name: str) -> dict[str, list[list[float]]]:
    lines = text.splitlines()
    out: dict[str, list[list[float]]] = {}
    i = 0
    while i < len(lines):
        if lines[i].strip().lower() != "loop_":
            i += 1
            continue
        i += 1
        tags: list[str] = []
        while i < len(lines) and lines[i].strip().startswith("_"):
            tags.append(lines[i].strip())
            i += 1
        if not all(tag.startswith("_atom_site.") for tag in tags):
            continue
        keys = [tag.split(".", 1)[1] for tag in tags]
        try:
            col_name = keys.index("label_atom_id")
            col_chain = keys.index("label_asym_id")
            col_x = keys.index("Cartn_x")
            col_y = keys.index("Cartn_y")
            col_z = keys.index("Cartn_z")
        except ValueError as exc:
            raise ValueError(
                "mmCIF _atom_site loop lacks label_atom_id / label_asym_id / "
                f"Cartn_x,y,z — cannot read a trace from it ({exc})"
            ) from None
        while i < len(lines):
            row = lines[i].strip()
            if not row or row.startswith(("#", "_", "loop_", "data_")):
                break
            values = _split_cif_row(row)
            i += 1
            if len(values) != len(keys):
                continue
            if values[col_name] != atom_name:
                continue
            out.setdefault(values[col_chain], []).append(
                [float(values[col_x]), float(values[col_y]), float(values[col_z])]
            )
    return out


def read_trace(text: str, atom_name: str) -> dict[str, np.ndarray]:
    """One atom's coordinates per chain — ``{chain_id: (n, 3)}`` angstroms.

    Reads PDB ``ATOM``/``HETATM`` records or an mmCIF ``_atom_site`` loop; the
    format is detected from the text (``_atom_site.`` present) rather than
    declared, because a caller handed a file by a fetcher often does not know.

    ``atom_name`` is matched against the whitespace-stripped atom-name field
    exactly — ``"CA"`` for a protein alpha carbon, ``"P"`` or ``"C1'"`` for a
    nucleic-acid backbone trace. Nothing else is matched for you: an
    ``atom_name`` that appears nowhere yields an empty dict rather than an
    error, since a chain legitimately may not carry the atom.

    Rows come back in file order, which for a well-formed model is residue
    order. Alternate locations, insertion codes and models are **not**
    distinguished — every matching record lands in its chain's array. A caller
    reading a multi-model NMR ensemble gets all models concatenated, and should
    split on ``MODEL`` itself before calling.
    """
    if "_atom_site." in text:
        collected = _read_trace_cif(text, atom_name)
    else:
        collected = {}
        for line in text.splitlines():
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            if len(line) < 54:
                continue
            if line[12:16].strip() != atom_name:
                continue
            chain = line[21:22].strip() or " "
            collected.setdefault(chain, []).append(
                [float(line[30:38]), float(line[38:46]), float(line[46:54])]
            )
    return {
        chain: np.array(rows, dtype=float).reshape(-1, 3)
        for chain, rows in collected.items()
    }
