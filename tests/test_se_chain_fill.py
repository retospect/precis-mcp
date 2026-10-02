"""``fill_complement`` and ``chain_sequence_length`` — the se-nucleic-chain
thread's staple-sequence item.

Scaffold sequence in, staple sequences out: each staple base is the
Watson–Crick complement of the derived co-occupant, and a base with no
partner letter (a loop, a single-stranded offset) is refused by name,
never invented.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se.chain.drc import findings
from precis_se.chain.pairing import PAIRED, derive_pairing
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

DNA = {"A": "T", "C": "G", "G": "C", "T": "A"}
#: 64 nt, deterministic, every letter present.
SCAFFOLD = "ATGCGTACCTAGGATCCGTTAACGGATCAGTCGACTTAGCATGCAAGCTTGGCCATAGCTAGCA"


def _revcomp(seq: str) -> str:
    return "".join(DNA[c] for c in reversed(seq))


def _domain(
    strand: str, helix: str, start: int, end: int, forward: bool, **extra: Any
) -> dict[str, Any]:
    return {
        "op": "add_domain",
        "strand": strand,
        "helix": helix,
        "start": start,
        "end": end,
        "forward": forward,
        **extra,
    }


def _tile_ops() -> list[dict[str, Any]]:
    """Four square-lattice helices × 16 bp, a 64-nt scaffold snaking
    through all four, and five unsequenced staples covering every scaffold
    base: three two-domain crossover staples and two single-domain ones."""
    ops: list[dict[str, Any]] = []
    for col in range(4):
        ops.append({"op": "add_block", "name": f"h{col}"})
        ops.append(
            {
                "op": "declare_helix",
                "block": f"h{col}",
                "n_units": 16,
                "lattice": "square",
                "row": 0,
                "col": col,
            }
        )
    ops.append({"op": "add_block", "name": "scaf"})
    ops.append({"op": "declare_strand", "block": "scaf", "sequence": SCAFFOLD})
    for col in range(4):
        extra = {"loop_before_nt": 0} if col else {}
        ops.append(_domain("scaf", f"h{col}", 0, 16, col % 2 == 0, **extra))
    staples = {
        "st0": [("h0", 8, 16, False), ("h1", 8, 16, True)],
        "st1": [("h1", 0, 8, True), ("h2", 0, 8, False)],
        "st2": [("h2", 8, 16, False), ("h3", 8, 16, True)],
        "st3": [("h0", 0, 8, False)],
        "st4": [("h3", 0, 8, True)],
    }
    for name, route in staples.items():
        ops.append({"op": "add_block", "name": name})
        ops.append({"op": "declare_strand", "block": name})
        for i, (helix, start, end, forward) in enumerate(route):
            extra = {"loop_before_nt": 0} if i else {}
            ops.append(_domain(name, helix, start, end, forward, **extra))
    return ops


def _tree(*extra: dict[str, Any]) -> SeTree:
    tree = SeTree()
    apply_ops(tree, [*_tile_ops(), *extra])
    return tree


def _seq(tree: SeTree, strand: str) -> str | None:
    return (tree.blocks[strand].chain or {}).get("sequence")


def _rules(tree: SeTree) -> list[str]:
    return [f.rule for f in findings(tree)]


def test_fill_writes_every_staple_as_the_complement_of_its_derived_partner() -> None:
    tree = _tree({"op": "fill_complement"})
    assert _seq(tree, "scaf") == SCAFFOLD  # the sequenced strand is untouched
    staples = ["st0", "st1", "st2", "st3", "st4"]
    assert all(_seq(tree, s) for s in staples)
    # st3 runs h0 7→0 against the scaffold's h0 0→7.
    assert _seq(tree, "st3") == _revcomp(SCAFFOLD[0:8])
    # Recomputed from the tree, independently of the op: every staple base
    # sits opposite a scaffold base it complements.
    pairing = derive_pairing(tree)
    staple_bases = 0
    for occ in pairing.offsets.values():
        assert occ.status == PAIRED
        by_strand = {o.strand: o.letter for o in occ.occupants}
        scaffold_letter = by_strand.pop("scaf")
        ((_staple, letter),) = by_strand.items()
        assert scaffold_letter is not None
        assert letter == DNA[scaffold_letter]
        staple_bases += 1
    assert staple_bases == len(SCAFFOLD)
    rules = _rules(tree)
    assert "chain_pairing_mismatch" not in rules
    assert "chain_sequence_length" not in rules


def test_fill_refuses_to_overwrite_and_the_bulk_form_needs_something_to_fill() -> None:
    tree = _tree({"op": "fill_complement"})
    with pytest.raises(OpError, match="already has a 16-nt sequence"):
        apply_ops(tree, [{"op": "fill_complement", "strand": "st0"}])
    with pytest.raises(OpError, match="every routed strand already has a sequence"):
        apply_ops(tree, [{"op": "fill_complement"}])
    with pytest.raises(OpError, match="need strand="):
        apply_ops(tree, [{"op": "fill_complement", "overwrite": True}])
    # Overwrite with strand= re-derives from the scaffold.
    apply_ops(
        tree,
        [
            {"op": "declare_strand", "block": "st3", "sequence": "A" * 8},
            {"op": "fill_complement", "strand": "st3", "overwrite": True},
        ],
    )
    assert _seq(tree, "st3") == _revcomp(SCAFFOLD[0:8])


def test_a_refused_second_target_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bulk fill vets every target before writing any: a refusal on
    st1 (after st0 vetted fine) leaves st0 unsequenced too."""
    from precis_se.chain import vocab as chain_vocab

    real = chain_vocab.vet_sequence

    def vet(sequence: Any, nucleic: str, where: str) -> str | None:
        if where.endswith(" st1"):
            raise chain_vocab.ChainError(f"{where}: refused for the test")
        return real(sequence, nucleic, where)

    monkeypatch.setattr(chain_vocab, "vet_sequence", vet)
    tree = _tree()
    with pytest.raises(OpError, match="fill_complement st1: refused for the test"):
        apply_ops(tree, [{"op": "fill_complement"}])
    assert [s for s in ("st0", "st1", "st2", "st3", "st4") if _seq(tree, s)] == []


def test_a_loop_with_no_letters_is_refused_by_name_and_loops_supplies_it() -> None:
    # st3 grows a second domain across to h1 through a 4-nt loop — on a
    # stretch st1 would otherwise hold, so take st1 off that stretch first.
    loop_ops = [
        {"op": "set_domain", "strand": "st1", "ord": 0, "start": 4, "end": 8},
        _domain("st3", "h1", 0, 4, True, loop_before_nt=4),
    ]
    tree = _tree(*loop_ops)
    with pytest.raises(OpError, match=r"st3: the 4-nt loop before #1"):
        apply_ops(tree, [{"op": "fill_complement", "strand": "st3"}])
    with pytest.raises(OpError, match="the loop is 4 nt, got 3"):
        apply_ops(
            tree, [{"op": "fill_complement", "strand": "st3", "loops": {"1": "TTT"}}]
        )
    with pytest.raises(OpError, match=r"no loop before #0\. Loops on this route: #1"):
        apply_ops(
            tree, [{"op": "fill_complement", "strand": "st3", "loops": {"0": "TTTT"}}]
        )
    apply_ops(tree, [{"op": "fill_complement", "strand": "st3", "loops": {1: "tttt"}}])
    # h1 runs 15→0 in the scaffold (reverse), so scaffold h1@k is SCAFFOLD[31-k].
    h1 = "".join(DNA[SCAFFOLD[31 - k]] for k in range(4))
    assert _seq(tree, "st3") == _revcomp(SCAFFOLD[0:8]) + "TTTT" + h1
    assert "chain_sequence_length" not in _rules(tree)


def test_a_single_stranded_offset_is_refused_unless_unknown_is_n() -> None:
    # st4 grows a 6-nt overhang onto a helix nothing else occupies.
    overhang: list[dict[str, Any]] = [
        {"op": "add_block", "name": "h4"},
        {
            "op": "declare_helix",
            "block": "h4",
            "n_units": 6,
            "lattice": "square",
            "row": 1,
            "col": 3,
        },
        _domain("st4", "h4", 0, 6, False, loop_before_nt=0),
    ]
    tree = _tree(*overhang)
    with pytest.raises(OpError, match=r"h4@5–0 \(single-stranded\)"):
        apply_ops(tree, [{"op": "fill_complement", "strand": "st4"}])
    with pytest.raises(OpError, match="'unknown' may only be 'N'"):
        apply_ops(tree, [{"op": "fill_complement", "strand": "st4", "unknown": "T"}])
    apply_ops(tree, [{"op": "fill_complement", "strand": "st4", "unknown": "N"}])
    # h3 runs 15→0 in the scaffold, so scaffold h3@k is SCAFFOLD[63-k].
    h3 = "".join(DNA[SCAFFOLD[63 - k]] for k in range(8))
    assert _seq(tree, "st4") == h3 + "N" * 6
    assert "chain_sequence_length" not in _rules(tree)


def test_an_unsequenced_partner_is_refused_rather_than_guessed() -> None:
    tree = _tree({"op": "declare_strand", "block": "scaf"})
    with pytest.raises(OpError, match=r"partner scaf has no letter"):
        apply_ops(tree, [{"op": "fill_complement", "strand": "st3"}])


def _duplex(fwd: str | None, register: dict[str, Any] | None, **strand: Any) -> SeTree:
    tree = SeTree()
    helix: dict[str, Any] = {
        "op": "declare_helix",
        "block": "h",
        "n_units": 8,
        "lattice": "square",
    }
    if register:
        helix["register"] = register
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            helix,
            {"op": "add_block", "name": "f"},
            {"op": "declare_strand", "block": "f", "sequence": fwd},
            {"op": "add_block", "name": "r"},
            {"op": "declare_strand", "block": "r", **strand},
            _domain("f", "h", 0, 8, True),
            _domain("r", "h", 0, 8, False),
            {"op": "fill_complement", "strand": "r"},
        ],
    )
    return tree


def test_an_rna_strand_gets_u_and_a_deleted_offset_gets_no_letter() -> None:
    tree = _duplex("AAAACCGT", None, nucleic="RNA")
    assert _seq(tree, "r") == "ACGGUUUU"
    tree = _duplex("AAAACGT", {"deletions": [3]})
    assert _seq(tree, "r") == _revcomp("AAAACGT")
    assert "chain_sequence_length" not in _rules(tree)


def test_an_inserted_offset_takes_the_partners_bases_complemented_in_reverse() -> None:
    # offset 5 carries two inserted bases: fwd reads A,A,A,A,A,(C,G,T),A,A
    fwd = "AAAAACGTAA"
    tree = _duplex(fwd, {"insertions": [5, 5]})
    assert _seq(tree, "r") == _revcomp(fwd)
    assert "chain_sequence_length" not in _rules(tree)


def test_a_wrong_length_sequence_fires_chain_sequence_length() -> None:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {"op": "declare_helix", "block": "h", "n_units": 9, "lattice": "square"},
            {"op": "add_block", "name": "s"},
            {"op": "declare_strand", "block": "s", "sequence": "A" * 17},
            _domain("s", "h", 0, 9, True),
        ],
    )
    (found,) = [f for f in findings(tree) if f.rule == "chain_sequence_length"]
    assert found.severity == "error" and found.subject == "s"
    assert "sequence is 17 nt but the route holds 9" in found.detail
    assert "the last 8 letter(s) sit on no base" in found.detail
    apply_ops(tree, [{"op": "declare_strand", "block": "s", "sequence": "A" * 5}])
    (short,) = [f for f in findings(tree) if f.rule == "chain_sequence_length"]
    assert "the last 4 base(s) get no letter" in short.detail
    apply_ops(tree, [{"op": "declare_strand", "block": "s"}])
    assert "chain_sequence_length" not in _rules(tree)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def test_view_chain_shows_route_nt_equal_to_sequence_after_the_fill(
    handler: SeHandler,
) -> None:
    ops = [*_tile_ops(), {"op": "fill_complement"}]
    handler.put(id="fill", text=json.dumps({"ops": ops}))
    body = handler.get(id="fill", view="chain").body
    # strands rows: strand,nucleic,route_nt,sequence,… (agent-table rows;
    # the separator is a comma or a tab depending on the table renderer)
    expected = {"scaf": 64, "st0": 16, "st1": 16, "st2": 16, "st3": 8, "st4": 8}
    for strand, nt in expected.items():
        row = rf"^\s*{strand}[,\t]DNA[,\t]{nt}[,\t]{nt} nt[,\t]"
        assert re.search(row, body, re.M), strand
    assert "letters: 64 complementary" in body
