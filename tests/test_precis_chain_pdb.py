"""precis_chain.pdb — write/read round trip and the fixed-column contract.

The round-trip theorem: coordinates survive ``write_pdb`` -> ``read_trace`` to
0.001 A (the format's ``%8.3f`` field, and no better), per chain, in order.
``write_mmcif_atom_site`` is written to the same three decimals so the same
assertion holds for it, and ``read_trace`` detects which format it was handed.

The column assertions are not pedantry: a PDB reader is a fixed-column parser,
so an atom name starting one column early silently becomes a different element
in every other tool.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis_chain.pdb import read_trace, write_mmcif_atom_site, write_pdb

_N = 24


def _model() -> tuple[
    list[str], np.ndarray, list[str], list[str], list[int], list[str]
]:
    """A two-chain, twelve-residue-per-chain phosphate/sugar trace. Coordinates
    are pre-rounded to three decimals so the 0.001 A claim is about the format's
    round trip, not about rounding twice."""
    rng = np.random.default_rng(3)
    coords = np.round(rng.uniform(-50.0, 50.0, (_N, 3)), 3)
    names = ["P" if i % 2 == 0 else "C1'" for i in range(_N)]
    elements = ["P" if i % 2 == 0 else "C" for i in range(_N)]
    resnames = ["DA"] * _N
    resseq = [i // 2 + 1 for i in range(_N)]
    chain_ids = ["A"] * 12 + ["B"] * 12
    return elements, coords, names, resnames, resseq, chain_ids


def _expected(atom_name: str) -> dict[str, np.ndarray]:
    _el, coords, names, _rn, _rs, chains = _model()
    out: dict[str, list[np.ndarray]] = {}
    for i in range(_N):
        if names[i] == atom_name:
            out.setdefault(chains[i], []).append(coords[i])
    return {chain: np.array(rows) for chain, rows in out.items()}


@pytest.mark.parametrize("atom_name", ["P", "C1'"])
def test_pdb_round_trip_is_exact_to_one_millianstrom(atom_name: str) -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    text = write_pdb(elements, coords, names, resnames, resseq, chains)
    trace = read_trace(text, atom_name)
    expected = _expected(atom_name)
    assert set(trace) == set(expected) == {"A", "B"}
    for chain in ("A", "B"):
        assert trace[chain].shape == expected[chain].shape
        assert np.max(np.abs(trace[chain] - expected[chain])) < 1e-3


@pytest.mark.parametrize("atom_name", ["P", "C1'"])
def test_mmcif_round_trip_is_exact_to_one_millianstrom(atom_name: str) -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    text = write_mmcif_atom_site(elements, coords, names, resnames, resseq, chains)
    assert "_atom_site.Cartn_x" in text
    trace = read_trace(text, atom_name)
    expected = _expected(atom_name)
    assert set(trace) == set(expected)
    for chain in ("A", "B"):
        assert np.max(np.abs(trace[chain] - expected[chain])) < 1e-3


def test_the_two_writers_agree_on_every_coordinate() -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    from_pdb = read_trace(
        write_pdb(elements, coords, names, resnames, resseq, chains), "P"
    )
    from_cif = read_trace(
        write_mmcif_atom_site(elements, coords, names, resnames, resseq, chains), "P"
    )
    for chain in from_pdb:
        assert np.allclose(from_pdb[chain], from_cif[chain])


def test_pdb_atom_records_land_in_the_right_columns() -> None:
    text = write_pdb(
        ["C", "P"],
        np.array([[1.234, -5.678, 90.123], [0.0, 0.0, 0.0]]),
        ["CA", "P"],
        ["ALA", "DA"],
        [7, 8],
        ["Z", "Z"],
    )
    line = text.splitlines()[0]
    assert len(line) == 78
    assert line[0:6] == "ATOM  "
    assert line[6:11] == "    1"
    assert line[12:16] == " CA "  # one-letter element indented by a space
    assert line[17:20] == "ALA"
    assert line[21] == "Z"
    assert line[22:26] == "   7"
    assert line[30:38] == "   1.234"
    assert line[38:46] == "  -5.678"
    assert line[46:54] == "  90.123"
    assert line[54:60] == "  1.00"
    assert line[76:78] == " C"
    assert text.splitlines()[-1] == "END"


def test_ter_records_mark_every_chain_change() -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    text = write_pdb(elements, coords, names, resnames, resseq, chains)
    ters = [line for line in text.splitlines() if line.startswith("TER")]
    assert len(ters) == 2
    assert ters[0][21] == "A"
    assert ters[1][21] == "B"


def test_read_trace_ignores_unknown_atoms_and_non_atom_lines() -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    text = write_pdb(elements, coords, names, resnames, resseq, chains)
    assert read_trace(text, "ZZ") == {}
    noisy = "HEADER    something\nREMARK  1 nothing here\n" + text
    assert set(read_trace(noisy, "P")) == {"A", "B"}


def test_read_trace_accepts_hetatm() -> None:
    elements, coords, names, resnames, resseq, chains = _model()
    text = write_pdb(elements, coords, names, resnames, resseq, chains)
    swapped = "\n".join(
        "HETATM" + line[6:] if line.startswith("ATOM  ") else line
        for line in text.splitlines()
    )
    assert np.allclose(read_trace(swapped, "P")["A"], _expected("P")["A"], atol=1e-3)


def test_mmcif_quotes_only_what_needs_it() -> None:
    text = write_mmcif_atom_site(
        ["C", "C"],
        np.zeros((2, 3)),
        ["C1'", "a b"],
        ["DA", "DA"],
        [1, 2],
        ["A", "A"],
    )
    rows = [line for line in text.splitlines() if line.startswith("ATOM")]
    assert " C1' " in rows[0]  # an apostrophe mid-token needs no quoting
    assert '"a b"' in rows[1]
    assert np.allclose(read_trace(text, "a b")["A"], np.zeros((1, 3)))


def test_writer_validation() -> None:
    coords = np.zeros((2, 3))
    with pytest.raises(ValueError, match="names has 1 entries"):
        write_pdb(["C", "C"], coords, ["CA"], ["ALA"] * 2, [1, 2], ["A"] * 2)
    with pytest.raises(ValueError, match=r"coords_A must be \(n, 3\)"):
        write_pdb(["C"], np.zeros((1, 2)), ["CA"], ["ALA"], [1], ["A"])
    with pytest.raises(ValueError, match="must be finite"):
        write_pdb(["C"], np.array([[np.nan, 0.0, 0.0]]), ["CA"], ["ALA"], [1], ["A"])
    with pytest.raises(ValueError, match="4-character field"):
        write_pdb(["C"], np.zeros((1, 3)), ["TOOLONG"], ["ALA"], [1], ["A"])
    with pytest.raises(ValueError, match="resSeq field"):
        write_pdb(["C"], np.zeros((1, 3)), ["CA"], ["ALA"], [100000], ["A"])
