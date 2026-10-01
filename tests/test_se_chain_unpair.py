"""The per-offset ``unpair`` op — owner ruling 2026-10-01
(``docs/backlog/se-chain-insertions-deletions.md`` §"Per-offset unpair").

The mark is a per-domain ``overrides[offset] = 'unpaired'``; the domain
list is untouched. One unpaired offset stays at its duplex position, a run
of them is a folding question and only warns.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se.chain import pairing
from precis_se.chain.drc import findings
from precis_se.chain.vocab import ChainError, vet_overrides
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops
from tests.test_se_chain_realize import (
    _atoms,
    _hairpin_ops,
    _loaded,
    _put,
    _realize,
    _seed_se_migrations,
)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


def _tree(ops: list[dict[str, Any]] | None = None) -> SeTree:
    tree = SeTree()
    apply_ops(tree, ops or _hairpin_ops())
    return tree


def _unpair(at: str, **extra: Any) -> dict[str, Any]:
    return {"op": "unpair", "at": at, **extra}


def _rules(tree: SeTree) -> dict[str, str]:
    return {f.rule: f.severity for f in findings(tree)}


def test_vet_overrides_accepts_unpaired_case_insensitively() -> None:
    out = vet_overrides({"1": " Unpaired ", "2": "wobble"}, "t", start=0, end=4)
    assert out is not None and out["1"] == "unpaired"
    assert out["2"] != "unpaired"
    with pytest.raises(ChainError, match="unknown pair geometry"):
        vet_overrides({"1": "unpair"}, "t", start=0, end=4)


def test_unpair_marks_both_domains_and_derive_pairing_reports_it() -> None:
    tree = _tree()
    before = len(tree.domains)
    apply_ops(tree, [_unpair("stem@1")])
    assert len(tree.domains) == before
    assert [(d.overrides or {}).get("1") for d in tree.domains] == [
        "unpaired",
        "unpaired",
    ]
    p = pairing.derive_pairing(tree)
    occ = p.at("stem", 1)
    assert occ is not None and occ.status == pairing.UNPAIRED
    assert len(occ.unpaired_by) == 2
    assert occ.geometry is None and occ.declarations == ()
    assert occ in p.unpaired and occ not in p.pairs
    assert len(p.pairs) == 3 and len(p.unpaired) == 1
    assert not p.singles and not p.conflicts


def test_unpair_replaces_a_geometry_override_and_clear_restores_the_pair() -> None:
    tree = _tree()
    apply_ops(
        tree,
        [{"op": "set_domain", "strand": "hp", "ord": 1, "overrides": {"2": "W-W-cis"}}],
    )
    apply_ops(tree, [_unpair("stem@2")])
    assert (tree.domains[1].overrides or {})["2"] == "unpaired"
    apply_ops(tree, [_unpair("stem@2", clear=True)])
    assert all("2" not in (d.overrides or {}) for d in tree.domains)
    occ = pairing.derive_pairing(tree).at("stem", 2)
    assert occ is not None and occ.status == pairing.PAIRED
    assert not pairing.derive_pairing(tree).unpaired


def test_unpair_refusals_name_what_is_there() -> None:
    tree = _tree()
    with pytest.raises(OpError, match="nothing occupies stem@9.*stem\\[0:4\\)"):
        apply_ops(tree, [_unpair("stem@9")])
    with pytest.raises(OpError, match="no such block.*Available blocks: hp, stem"):
        apply_ops(tree, [_unpair("nosuch@1")])
    with pytest.raises(OpError, match="nothing occupies hp@1"):
        apply_ops(tree, [_unpair("hp@1")])
    with pytest.raises(OpError, match="no domain marks stem@1 unpaired"):
        apply_ops(tree, [_unpair("stem@1", clear=True)])
    with pytest.raises(OpError, match="'<helix>@<offset>'"):
        apply_ops(tree, [{"op": "unpair", "at": "stem"}])
    with pytest.raises(OpError, match="needs 'at'"):
        apply_ops(tree, [{"op": "unpair"}])


def test_unpair_refuses_a_single_occupant_and_a_parallel_pair() -> None:
    tree = _tree()
    # Shorten the second domain's helix coverage by replacing it with a
    # parallel one: offset 1 is then a chain_occupancy problem, not a pair.
    apply_ops(
        tree,
        [{"op": "set_domain", "strand": "hp", "ord": 1, "forward": True}],
    )
    with pytest.raises(OpError, match="chain_occupancy"):
        apply_ops(tree, [_unpair("stem@1")])
    single = _tree()
    apply_ops(
        single,
        [
            {"op": "add_block", "name": "tail"},
            {"op": "declare_strand", "block": "tail"},
            {
                "op": "add_domain",
                "strand": "tail",
                "helix": "stem",
                "start": 4,
                "end": 6,
                "forward": True,
            },
        ],
    )
    # n_units is 4, so the domain dangles — but it is the only occupant.
    with pytest.raises(OpError, match="already single-stranded"):
        apply_ops(single, [_unpair("stem@5")])


def test_one_unpaired_offset_is_info_and_not_a_mismatch() -> None:
    ops = [
        op | {"sequence": "GGGAAAAACCCC"}
        if op.get("op") == "declare_strand"
        else {k: v for k, v in op.items() if k != "geometry"}
        for op in _hairpin_ops()
    ]
    tree = _tree(ops)
    assert "chain_pairing_mismatch" in _rules(tree)  # A·C at offset 3
    apply_ops(tree, [_unpair("stem@3")])
    found = {f.rule: f for f in findings(tree)}
    assert "chain_pairing_mismatch" not in found
    one = found["chain_unpaired"]
    assert one.severity == "info" and one.subject == "stem[3]"
    assert "hp#0 A" in one.detail and "hp#1 C" in one.detail
    assert "duplex position" in one.detail
    assert "chain_unpaired_run" not in found and "chain_unpaired_stray" not in found


def test_two_consecutive_unpaired_offsets_warn_as_a_folding_question() -> None:
    tree = _tree()
    apply_ops(tree, [_unpair("stem@1"), _unpair("stem@2")])
    found = {f.rule: f for f in findings(tree)}
    run = found["chain_unpaired_run"]
    assert run.severity == "warn" and run.subject == "stem[1:3]"
    assert "2 consecutive unpaired offsets" in run.detail
    assert "ViennaRNA" in run.detail and "oxDNA" in run.detail
    assert "chain_unpaired" not in found
    # A gap splits the run: two singletons, no warning.
    gap = _tree()
    apply_ops(gap, [_unpair("stem@0"), _unpair("stem@2")])
    rules = [f.rule for f in findings(gap)]
    assert rules.count("chain_unpaired") == 2 and "chain_unpaired_run" not in rules


def test_a_mark_whose_partner_moved_away_is_a_stray() -> None:
    tree = _tree()
    apply_ops(tree, [_unpair("stem@1")])
    # Flipping one domain parallel keeps the override (it stays in range)
    # but makes the offset a chain_occupancy conflict, not a pair.
    apply_ops(tree, [{"op": "set_domain", "strand": "hp", "ord": 1, "forward": True}])
    found = {f.rule: f for f in findings(tree)}
    stray = found["chain_unpaired_stray"]
    assert stray.severity == "warn" and stray.subject == "stem[1]"
    assert "parallel" in stray.detail and "unpair(clear=true)" in stray.detail
    assert "chain_unpaired" not in found


def test_chain_and_drc_views_count_the_unpaired(handler: SeHandler) -> None:
    ops = [*_hairpin_ops(), _unpair("stem@1")]
    handler.put(id="up", text=json.dumps({"ops": ops}))
    body = handler.get(id="up", view="chain").body
    assert "3 paired offset(s) · 1 unpaired" in body
    assert "3 paired · 0 single · 0 free · 1 unpaired" in body
    assert "letters: 3 complementary" in body
    drc = handler.get(id="up", view="drc").body
    assert "chain_unpaired" in drc and "stem[1]" in drc


def test_pick_labels_an_unpaired_offset_as_a_pair_row(
    handler: SeHandler, store: Store
) -> None:
    _put(handler, "pk", [*_hairpin_ops(), _unpair("stem@1")])
    tree = _loaded(store, "pk")
    uid = {name: int(node.uid or 0) for name, node in tree.blocks.items()}
    body = handler.get(
        id="pk", view="pick", args={"token": f"<se:{uid['stem']}@1>"}
    ).body
    row = next(line for line in body.splitlines() if "stem@1" in line)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells[0] == "pair"
    assert cells[1] == "stem@1 (G·C, unpaired: hp.0 / hp.1)"
    assert cells[2] == f"`<se:{uid['stem']}@1>`"


def test_realize_chain_still_places_both_nucleotides_of_an_unpaired_offset(
    handler: SeHandler, store: Store
) -> None:
    echo = _put(handler, "plain", [*_hairpin_ops(), _realize("stem", 0, 4)])
    echo_up = _put(
        handler, "marked", [*_hairpin_ops(), _unpair("stem@1"), _realize("stem", 0, 4)]
    )
    assert "8 nucleotide(s)" in echo and "8 nucleotide(s)" in echo_up
    plain_coords, plain_meta = _atoms(store, "plain-stem.s0")
    coords, meta = _atoms(store, "marked-stem.s0")
    assert coords.shape == plain_coords.shape
    assert sorted(set(meta["resseq"])) == sorted(set(plain_meta["resseq"]))
    assert max(meta["resseq"]) == 8
