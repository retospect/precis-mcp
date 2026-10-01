"""Register insertions/deletions — slice 1 of
``docs/backlog/se-chain-insertions-deletions.md``.

The frames stay at the lattice twist (the caDNAno convention); what
insertions/deletions retune is the *real-twist account*
(:func:`precis_se.chain.layout.twist_residual`). Every expected number is
recomputed from the motifs, never a literal.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from precis.errors import BadInput, Unsupported
from precis_chain.register import phase_after
from precis_se.chain import nucleic
from precis_se.chain.drc import findings
from precis_se.chain.export import to_cadnano, to_oxdna, to_scadnano
from precis_se.chain.layout import helix_geometry, twist_residual
from precis_se.chain.pairing import (
    PAIRED,
    derive_pairing,
    helix_indels,
    strand_length_nt,
    strand_letters,
    watson_crick,
)
from precis_se.chain.vocab import group_domains
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.ops_export import design_ops
from tests.test_se_chain_realize import (
    _duplex_ops,
    _put,
    _realize,
    handler,  # noqa: F401  (fixture)
)

#: Lattice twist per base on the square lattice: 3 turns / 32 units.
SQUARE_TWIST = 2.0 * math.pi * 3 / 32
BASE_TWIST = nucleic.B_DNA_TWIST_RAD


def _by_rule(tree: SeTree, rule: str) -> list[Any]:
    return [f for f in findings(tree) if f.rule == rule]


def _sheet(
    n_helices: int = 6,
    n_units: int = 64,
    *,
    register: dict[str, Any] | None = None,
    lattice: str = "square",
    connect: bool = True,
) -> SeTree:
    """``n_helices`` lattice helices side by side on row 0; with ``connect``
    one strand per neighbouring pair makes them one connected set."""
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for i in range(n_helices):
        helix: dict[str, Any] = {
            "op": "declare_helix",
            "block": f"h{i}",
            "n_units": n_units,
            "lattice": lattice,
            "row": 0,
            "col": i,
        }
        if register is not None:
            helix["register"] = register
        ops += [{"op": "add_block", "name": f"h{i}"}, helix]
    if connect:
        for i in range(n_helices - 1):
            ops += [
                {"op": "add_block", "name": f"s{i}"},
                {"op": "declare_strand", "block": f"s{i}"},
                {
                    "op": "add_domain",
                    "strand": f"s{i}",
                    "helix": f"h{i}",
                    "start": 0,
                    "end": 8,
                    "forward": True,
                },
                {
                    "op": "add_domain",
                    "strand": f"s{i}",
                    "helix": f"h{i + 1}",
                    "start": 0,
                    "end": 8,
                    "forward": False,
                    "loop_before_nt": 0,
                },
            ]
    apply_ops(tree, ops)
    return tree


# ── (a)/(b) the global twist finding ────────────────────────────────────


def test_an_uncorrected_sheet_names_its_residual_per_helix() -> None:
    tree = _sheet()
    fired = _by_rule(tree, "chain_twist_global")
    assert len(fired) == 1
    assert fired[0].severity == "warn"
    expected = math.degrees(64 * BASE_TWIST - 64 * SQUARE_TWIST)
    assert 30.0 < expected < 40.0  # the ~+34 deg the item names
    for i in range(6):
        residual = twist_residual(helix_geometry(tree.blocks[f"h{i}"]))
        assert math.degrees(residual) == pytest.approx(expected, abs=1e-9)
        assert f"h{i} {expected:+.1f} deg" in fired[0].detail
    assert "1 deletion(s) more would cancel" in fired[0].detail


def test_one_deletion_per_helix_brings_the_sheet_flat() -> None:
    tree = _sheet(register={"deletions": [10]})
    assert _by_rule(tree, "chain_twist_global") == []
    geom = helix_geometry(tree.blocks["h0"])
    # residual after one deletion: the lattice mismatch minus one base's
    # twist — under half a base's twist, the finding's tolerance.
    assert abs(twist_residual(geom)) < 0.5 * BASE_TWIST
    assert twist_residual(geom) == pytest.approx(
        64 * BASE_TWIST - 64 * SQUARE_TWIST - BASE_TWIST
    )


def test_a_free_helix_has_no_residual_and_the_frames_do_not_move() -> None:
    plain = helix_geometry(_sheet(register=None).blocks["h0"])
    edited = helix_geometry(_sheet(register={"deletions": [10]}).blocks["h0"])
    assert np.array_equal(plain.units.origins, edited.units.origins)
    assert np.array_equal(plain.units.frames, edited.units.frames)
    assert edited.deletions == 1 and plain.deletions == 0


def test_an_insertion_goes_the_other_way() -> None:
    # Two inserted bases on a helix that is already over-wound: the
    # residual grows by exactly two base twists.
    base = twist_residual(helix_geometry(_sheet().blocks["h0"]))
    more = twist_residual(
        helix_geometry(_sheet(register={"insertions": [4, 4]}).blocks["h0"])
    )
    assert more - base == pytest.approx(2 * BASE_TWIST)


# ── (c) chain_twist_register needs a neighbour ──────────────────────────


def test_a_lone_helix_is_not_out_of_register_but_a_connected_pair_is() -> None:
    lone = _sheet(n_helices=1, n_units=22, lattice="honeycomb", connect=False)
    assert _by_rule(lone, "chain_twist_register") == []
    assert _by_rule(lone, "chain_twist_global") == []
    pair = _sheet(n_helices=2, n_units=22, lattice="honeycomb")
    assert [f.subject for f in _by_rule(pair, "chain_twist_register")] == ["h0", "h1"]
    # two helices never joined by a strand are two lone helices
    apart = _sheet(n_helices=2, n_units=22, lattice="honeycomb", connect=False)
    assert _by_rule(apart, "chain_twist_register") == []


# ── (d) vetting ─────────────────────────────────────────────────────────


def _register(tree: SeTree) -> dict[str, Any]:
    chain = tree.blocks["h"].chain
    assert chain is not None
    return dict(chain["register"])


def _declare(register: Any, n_units: int = 16) -> SeTree:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": n_units,
                "lattice": "square",
                "register": register,
            },
        ],
    )
    return tree


def test_register_lists_are_vetted_and_stored_sorted() -> None:
    tree = _declare({"insertions": [9, 3, 3], "deletions": [7, 1]})
    assert _register(tree) == {
        "lattice": "square",
        "insertions": [3, 3, 9],
        "deletions": [1, 7],
    }
    # empty lists are not stored
    assert _register(_declare({"insertions": [], "deletions": []})) == {
        "lattice": "square"
    }


@pytest.mark.parametrize(
    ("register", "message"),
    [
        ({"deletions": [16]}, "outside"),
        ({"insertions": [-1]}, "must be >= 0"),
        ({"deletions": [2, 2]}, "more than once"),
        ({"insertions": [2], "deletions": [2]}, "both"),
        ({"deletions": "3"}, "list of helix offsets"),
        ({"deletions": [1.5]}, "whole number"),
    ],
)
def test_vet_register_refusals(register: dict[str, Any], message: str) -> None:
    with pytest.raises(OpError, match=message):
        _declare(register)


def test_a_repeated_insertion_offset_is_allowed() -> None:
    _declare({"insertions": [2, 2, 2]})


def test_register_round_trips_through_ops_export() -> None:
    tree = _declare({"insertions": [3, 3], "deletions": [5]})
    again = SeTree()
    apply_ops(again, design_ops(tree))
    assert _register(again) == _register(tree)


# ── (e) sequence accounting and pairing ─────────────────────────────────


def _duplex(
    fwd: str,
    rev: str,
    register: dict[str, Any] | None,
    n_units: int = 8,
) -> SeTree:
    tree = SeTree()
    helix: dict[str, Any] = {
        "op": "declare_helix",
        "block": "h",
        "n_units": n_units,
        "lattice": "square",
    }
    if register is not None:
        helix["register"] = register
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            helix,
            {"op": "add_block", "name": "f"},
            {"op": "declare_strand", "block": "f", "sequence": fwd},
            {"op": "add_block", "name": "r"},
            {"op": "declare_strand", "block": "r", "sequence": rev},
            {
                "op": "add_domain",
                "strand": "f",
                "helix": "h",
                "start": 0,
                "end": n_units,
                "forward": True,
            },
            {
                "op": "add_domain",
                "strand": "r",
                "helix": "h",
                "start": 0,
                "end": n_units,
                "forward": False,
            },
        ],
    )
    return tree


def test_a_deletion_consumes_no_letter_and_shifts_the_rest() -> None:
    fwd = "ACGTACG"  # 8 offsets, one deleted
    rev = "".join({"A": "T", "C": "G", "G": "C", "T": "A"}[c] for c in reversed(fwd))
    tree = _duplex(fwd, rev, {"deletions": [3]})
    indels = helix_indels(tree)
    route = group_domains(tree.domains).by_strand["f"]
    assert strand_length_nt(route) == 8  # without the register: wrong
    assert strand_length_nt(route, indels) == len(fwd) == 7
    letters = strand_letters(fwd, route, indels)
    assert (0, 3) not in letters
    assert [letters[(0, o)] for o in (0, 1, 2, 4, 5, 6, 7)] == list(fwd)

    pairing = derive_pairing(tree)
    assert pairing.at("h", 3) is None
    for o in (0, 1, 2, 4, 5, 6, 7):
        occ = pairing.at("h", o)
        assert occ is not None and occ.status == PAIRED
        assert watson_crick(occ) is True
    mismatches = [f for f in findings(tree) if f.rule == "chain_pairing_mismatch"]
    assert mismatches == []


def test_an_insertion_consumes_extra_letters_and_is_unverifiable() -> None:
    # offset 5 carries two inserted bases -> 3 letters there
    fwd = "AAAAAAAAAA"  # 8 + 2
    # reverse strand reads 7,6,5,5',5'',4..0: letters 2..4 sit at the
    # insertion; make them non-complementary so a naive check would fire.
    rev = "TT" + "GGG" + "TTTTT"
    tree = _duplex(fwd, rev, {"insertions": [5, 5]})
    indels = helix_indels(tree)
    route_f = group_domains(tree.domains).by_strand["f"]
    route_r = group_domains(tree.domains).by_strand["r"]
    assert strand_length_nt(route_f, indels) == len(fwd) == 10
    assert strand_length_nt(route_r, indels) == len(rev) == 10
    letters = strand_letters(fwd, route_f, indels)
    assert (0, 5) not in letters  # unverifiable, not guessed
    assert letters[(0, 6)] == "A" and letters[(0, 7)] == "A"
    rev_letters = strand_letters(rev, route_r, indels)
    assert rev_letters[(0, 7)] == "T" and rev_letters[(0, 6)] == "T"
    assert (0, 5) not in rev_letters
    assert rev_letters[(0, 4)] == "T"  # letter index 5, after the 3 at offset 5
    occ = derive_pairing(tree).at("h", 5)
    assert occ is not None and occ.status == PAIRED
    assert watson_crick(occ) is None
    assert [f for f in findings(tree) if f.rule == "chain_pairing_mismatch"] == []


# ── (f) realize_chain ───────────────────────────────────────────────────


def _with_register(register: dict[str, Any]) -> list[dict[str, Any]]:
    ops = _duplex_ops()
    for op in ops:
        if op.get("op") == "declare_helix":
            op["register"] = register
    return ops


def test_realize_chain_refuses_a_region_holding_a_deletion(handler) -> None:  # noqa: F811
    ops = _with_register({"deletions": [10]})
    with pytest.raises(Unsupported, match=r"insertion/deletion offset\(s\) \[10\]"):
        _put(handler, "holds", [*ops, _realize("h", 0, 21)])
    try:
        _put(handler, "beside", [*ops, _realize("h", 0, 10)])
    except BadInput as exc:  # pragma: no cover - any other refusal is a bug
        pytest.fail(f"a region beside the deletion must not be refused: {exc}")


# ── (g) the kernel hook ─────────────────────────────────────────────────


def test_phase_after_applies_per_unit_twist() -> None:
    motif = nucleic.MOTIFS["B-DNA"]
    n = 21
    nominal = phase_after(motif, n)
    extra = np.zeros(n)
    assert phase_after(motif, n, per_unit_twist=extra) == pytest.approx(nominal)
    extra[3] = motif.twist  # one inserted base
    extra[9] = -motif.twist  # one deleted
    extra[12] = -motif.twist  # another deleted
    got = phase_after(motif, n, per_unit_twist=extra)
    want = (n * motif.twist - motif.twist) % (2 * math.pi)
    want = want - 2 * math.pi if want >= math.pi else want
    assert got == pytest.approx(want)
    with pytest.raises(ValueError, match="shape"):
        phase_after(motif, n, per_unit_twist=np.zeros(n + 1))


# ── (h) exporters ───────────────────────────────────────────────────────


def test_scadnano_and_cadnano_carry_the_register() -> None:
    tree = _sheet(
        n_helices=2, n_units=32, register={"deletions": [10], "insertions": [4, 4]}
    )
    doc = json.loads(json.dumps(to_scadnano(tree, design="d")))
    assert doc["helices"][0]["deletions"] == [10]
    assert doc["helices"][0]["insertions"] == [[4, 2]]
    cad = to_cadnano(tree, design="d")
    for vstrand in cad["vstrands"]:
        assert vstrand["skip"][10] == -1
        assert sum(1 for v in vstrand["skip"] if v) == 1
        assert vstrand["loop"][4] == 2
    plain = to_scadnano(_sheet(n_helices=2, n_units=32), design="d")
    assert "deletions" not in plain["helices"][0]


def test_oxdna_skips_a_deleted_base_and_refuses_an_insertion() -> None:
    with_del = _duplex("ACGTACG", "CGTACGT", {"deletions": [3]})
    top, _conf = to_oxdna(with_del, design="d")
    assert top.splitlines()[0] == "14 2"  # 7 + 7 nucleotides, not 8 + 8
    with_ins = _duplex("A" * 9, "T" * 9, {"insertions": [3]})
    with pytest.raises(Unsupported, match="register insertion"):
        to_oxdna(with_ins, design="d")
