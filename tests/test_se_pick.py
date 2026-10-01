"""``view='pick'`` and :mod:`precis_se.pick` — an atom of a realized chain
resolves to every level it belongs to, and each row's token reads back
(``docs/backlog/se-pick-hierarchy.md``, the chain-design instance)."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import pick
from precis_se.atomic.render import bound_pick_inputs
from precis_se.handler import SeHandler
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


def _hairpin(handler: SeHandler, slug: str) -> None:
    _put(
        handler,
        slug,
        [*_hairpin_ops(), {"op": "relax_chain"}, _realize("stem", 0, 4, loops=True)],
    )


def _rows(body: str) -> list[tuple[str, str, str]]:
    """``(level, what, token)`` per table row of a ``view='pick'`` body."""
    out = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2].startswith("`<se:"):
            out.append((cells[0], cells[1], cells[2].strip("`")))
    return out


def _ordinal(meta: dict[str, object], name: str, resseq: int) -> int:
    names, seqs = meta["names"], meta["resseq"]
    assert isinstance(names, list) and isinstance(seqs, list)
    return next(
        i
        for i, (n, s) in enumerate(zip(names, seqs, strict=True))
        if n == name and s == resseq
    )


def test_token_grammar_round_trips_and_refuses_junk() -> None:
    for token, ref in [
        ("<se:41>", pick.PickRef(41)),
        ("<se:41#44>", pick.PickRef(41, atom=44)),
        ("<se:41/A.8>", pick.PickRef(41, region="A.8")),
        ("<se:38/d1>", pick.PickRef(38, region="d1")),
        ("<se:40@3>", pick.PickRef(40, offset=3)),
    ]:
        assert pick.parse_token(token) == ref
        assert (
            pick.format_token(
                ref.uid, atom=ref.atom, region=ref.region, offset=ref.offset
            )
            == token
        )
    # '@' is a helix offset and nothing else — no datum, no base letter.
    for junk in (
        "se:41",
        "<se:stem>",
        "<se:41#x>",
        "<se:41#1/A.8>",
        "<nm:41>",
        "<se:41@axis>",
        "<se:41@3G>",
    ):
        with pytest.raises(pick.PickError, match="not an se reference token"):
            pick.parse_token(junk)


def test_a_stem_atom_resolves_through_residue_pair_domain_strand_and_blocks(
    handler: SeHandler, store: Store
) -> None:
    _hairpin(handler, "pk")
    tree = _loaded(store, "pk")
    _coords, meta = _atoms(store, "pk-stem.s0")
    # The O3' of the strand's 4th nucleotide: stem offset 3, the pair the
    # loop closes on (G·C with the strand's 9th nucleotide).
    ordinal = _ordinal(meta, "O3'", 4)
    rows = _rows(
        handler.get(
            id="pk", view="pick", args={"block": "stem.s0", "atom": ordinal}
        ).body
    )
    uid = {name: int(node.uid or 0) for name, node in tree.blocks.items()}
    assert [r[0] for r in rows] == [
        "atom",
        "residue",
        "pair",
        "domain",
        "strand",
        "segment",
        "helix",
    ]
    assert rows[0][1:] == ("O3' of DG 4", f"<se:{uid['stem.s0']}#{ordinal}>")
    assert rows[1][1:] == ("DG 4 (chain A)", f"<se:{uid['stem.s0']}/A.4>")
    assert rows[2][1:] == (
        "stem@3 (G·C, paired: hp.0 / hp.1)",
        f"<se:{uid['stem']}@3>",
    )
    assert rows[3][1:] == ("hp.0 (stem[0, 4) 5'→3')", f"<se:{uid['hp']}/d0>")
    assert rows[4][1:] == ("hp", f"<se:{uid['hp']}>")
    assert rows[5][1:] == ("stem.s0", f"<se:{uid['stem.s0']}>")
    assert rows[6][1:] == ("stem", f"<se:{uid['stem']}>")

    # The scene label a finding prints addresses the same atom.
    ref = store.get_ref(kind="structure", id="pk-stem.s0")
    assert ref is not None
    scene, _handles = store.structure_load(ref.id)
    label = list(scene.atoms)[ordinal]
    by_label = handler.get(
        id="pk", view="pick", args={"block": f"#{uid['stem.s0']}", "atom": label}
    ).body
    assert _rows(by_label) == rows

    # Every row's token reads back to that row, followed only by levels
    # the atom's own list already holds.
    for i, (_level, _what, token) in enumerate(rows):
        back = _rows(handler.get(id="pk", view="pick", args={"token": token}).body)
        assert back[0] == rows[i], token
        assert set(back) <= set(rows), token


def test_a_loop_nucleotide_has_no_pair_row(handler: SeHandler, store: Store) -> None:
    _hairpin(handler, "pk-loop")
    _coords, meta = _atoms(store, "pk-loop-stem.s0")
    ordinal = _ordinal(meta, "P", 6)
    rows = _rows(
        handler.get(
            id="pk-loop", view="pick", args={"block": "stem.s0", "atom": ordinal}
        ).body
    )
    # In no domain either: the loop sits between hp.0 and hp.1.
    assert [r[0] for r in rows] == ["atom", "residue", "strand", "segment", "helix"]
    assert rows[1][1] == "DA 6 (loop nucleotide after hp.0)"


def test_a_record_without_residue_rows_says_why_pair_and_domain_are_missing(
    handler: SeHandler, store: Store
) -> None:
    # Prod structures realized before residue rows were persisted carry
    # the atom columns only; the pick names the residue and says why the
    # pair and domain rows stop there instead of dropping them silently.
    _hairpin(handler, "pk-old")
    tree = _loaded(store, "pk-old")
    node = tree.blocks["stem.s0"]
    labels, record = bound_pick_inputs(store, node)
    assert labels is not None and record is not None
    legacy = {k: v for k, v in record.items() if k != "residues"}
    ordinal = _ordinal(legacy, "O3'", 4)
    rows = pick.atom_levels(tree, node, ordinal, labels=labels, record=legacy)
    assert [r.level for r in rows] == ["atom", "residue", "segment", "helix"]
    assert "re-realize" in rows[1].label
    back = pick.resolve_token(
        tree, pick.parse_token(rows[1].token), labels=labels, record=legacy
    )
    assert back == rows[1:]


def test_pick_refusals_say_what_would_resolve(handler: SeHandler, store: Store) -> None:
    _hairpin(handler, "pk-bad")
    tree = _loaded(store, "pk-bad")
    uid = {name: int(node.uid or 0) for name, node in tree.blocks.items()}
    n_atoms = len(_atoms(store, "pk-bad-stem.s0")[1]["names"])

    def refused(args: dict[str, object], match: str) -> None:
        with pytest.raises(BadInput, match=match):
            handler.get(id="pk-bad", view="pick", args=args)

    refused({}, "takes args=")
    refused({"block": "stem.s0"}, "takes args=")
    refused({"block": "stem.s0", "atom": 0, "token": "<se:1>"}, "takes args=")
    refused({"block": "stem.s0", "atom": n_atoms}, re.escape(f"0..{n_atoms - 1}"))
    refused({"block": "stem.s0", "atom": str(n_atoms)}, re.escape(f"0..{n_atoms - 1}"))
    refused({"block": "stem.s0", "atom": "zz9"}, "no atom labelled 'zz9'")
    refused({"block": "hp", "atom": 0}, re.escape(f"'token': '<se:{uid['hp']}>'"))
    refused({"token": "<se:999999>"}, "no block with uid 999999.*view='block'")
    refused({"token": f"<se:{uid['hp']}#0>"}, "names no atom")
    refused({"token": f"<se:{uid['hp']}/d7>"}, "has no domain 7")
    refused({"token": f"<se:{uid['stem.s0']}/A.99>"}, "no residue A.99")
    refused({"token": f"<se:{uid['stem.s0']}/post>"}, "does not resolve")
    refused({"token": f"<se:{uid['stem.s0']}@3>"}, "is not a helix")

    # An unoccupied offset is an answer, not a refusal.
    rows = _rows(
        handler.get(
            id="pk-bad", view="pick", args={"token": f"<se:{uid['stem']}@9>"}
        ).body
    )
    assert rows[0][:2] == ("offset", "stem@9 (unoccupied)")


def test_atom_hover_names_read_the_record_or_fall_back_to_labels() -> None:
    labels = ["aP1", "aO2"]
    record = {
        "names": ["P", "OP1"],
        "resnames": ["DG", "DG"],
        "resseq": [4, 4],
        "chain_ids": ["A", "A"],
    }
    assert pick.atom_hover_names(labels, record) == ["P · DG 4 (A)", "OP1 · DG 4 (A)"]
    # No record, or columns that do not line up with the scene: labels.
    assert pick.atom_hover_names(labels) == labels
    assert pick.atom_hover_names(labels, {**record, "names": ["P"]}) == labels
