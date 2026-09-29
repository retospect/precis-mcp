"""``fold_layout`` and the fold findings — docs/backlog/se-nucleic-acid.md
slice 2's last pass.

The acceptance criteria this file IS: ``fold_layout`` on ``GGGGAAAACCCC``
yields one 4 bp helix, one strand, two antiparallel domains and a 4-nt loop,
and ``derive_pairing`` reports four ``W-W-cis``; ``RNA`` unimportable →
``view='drc'`` still renders with exactly ONE ``chain_fold_unavailable``;
installed → the hairpin's MFE is ``((((....))))`` and a planted 8-nt
staple–staple complement fires ``chain_offtarget``; a 6 kb scaffold is
skipped by the fold check with its length named; and ``fold_layout`` is
handler-level, a proposal in the web turn, and skipped by the pure dry run.

**Two tiers of test.** Everything that needs the real library
``importorskip("RNA")`` — ViennaRNA is the optional ``[chain]`` extra and
the dev image does not carry it yet. The *unavailable* branch is tested
unconditionally by making ``import RNA`` fail
(``sys.modules['RNA'] = None``, which the import system turns into an
``ImportError``), because that is the branch prod is in until the serve
role gets the extra.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.store import Store
from precis_se.atomic.apply import HANDLER_LEVEL_OPS, all_op_names
from precis_se.chain import fold as chain_fold
from precis_se.chain import nucleic
from precis_se.chain.pairing import derive_pairing
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops, known_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: The item's hairpin.
HAIRPIN = "GGGGAAAACCCC"
#: Its MFE structure — the criterion's own string.
HAIRPIN_MFE = "((((....))))"

_COMPLEMENT = {"A": "T", "T": "A", "G": "C", "C": "G"}


def _rc(seq: str) -> str:
    return "".join(_COMPLEMENT[c] for c in reversed(seq))


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


@pytest.fixture
def no_rna(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import RNA`` fail the way a venv without the ``[chain]``
    extra does — ``None`` in ``sys.modules`` is an ``ImportError`` at the
    import statement, so the real ``try``/``except`` in
    :func:`precis_se.chain.fold.rna_module` is what runs."""
    monkeypatch.setitem(sys.modules, "RNA", None)


def _straight_helix_ops(name: str, x_m: float, n_units: int) -> list[dict[str, Any]]:
    length = max(n_units - 1, 1) * nucleic.B_DNA_RISE_M
    return [
        {"op": "add_block", "name": name},
        {
            "op": "declare_helix",
            "block": name,
            "n_units": n_units,
            "path": {
                "waypoints": [
                    [f"{x_m} m", "0 m", "0 m"],
                    [f"{x_m} m", "0 m", f"{length} m"],
                ]
            },
        },
    ]


def _fold(op: dict[str, Any], tree: SeTree | None = None) -> tuple[SeTree, str]:
    tree = tree if tree is not None else SeTree()
    return tree, chain_fold.op_fold_layout(tree, {"op": "fold_layout", **op})


def _rules(rows: list[Any]) -> list[str]:
    return [r.rule for r in rows]


# ── the hairpin criterion ───────────────────────────────────────────────


def test_hairpins_mfe_is_the_criterions_dot_bracket() -> None:
    rna = pytest.importorskip("RNA")
    structure, energy = chain_fold.mfe_fold(rna, HAIRPIN)
    assert structure == HAIRPIN_MFE
    assert energy < 0.0


def test_fold_layout_lays_the_hairpin_out_as_one_helix_two_domains_one_loop() -> None:
    pytest.importorskip("RNA")
    tree, echo = _fold({"strand": "hp", "sequence": HAIRPIN})
    helices = [
        name
        for name, node in tree.blocks.items()
        if (node.chain or {}).get("role") == "helix"
    ]
    strands = [
        name
        for name, node in tree.blocks.items()
        if (node.chain or {}).get("role") == "strand"
    ]
    assert helices == ["hp.h0"]
    assert strands == ["hp"]
    assert (tree.blocks["hp.h0"].chain or {})["n_units"] == 4
    route = sorted(tree.domains, key=lambda d: d.ord)
    assert len(route) == 2
    assert [d.forward for d in route] == [True, False]
    assert {(d.start, d.end) for d in route} == {(0, 4)}
    assert route[0].loop_before_nt is None
    assert route[1].loop_before_nt == 4
    assert HAIRPIN_MFE in echo


def test_the_hairpins_derived_pairing_is_four_w_w_cis() -> None:
    pytest.importorskip("RNA")
    tree, _echo = _fold({"strand": "hp", "sequence": HAIRPIN})
    pairing = derive_pairing(tree)
    assert len(pairing.pairs) == 4
    assert [occ.geometry for occ in pairing.pairs] == ["W-W-cis"] * 4
    assert not pairing.conflicts
    assert not pairing.singles


def test_a_fold_layout_design_does_not_disagree_with_its_own_mfe() -> None:
    pytest.importorskip("RNA")
    tree, _echo = _fold({"strand": "hp", "sequence": HAIRPIN})
    assert chain_fold.findings(tree) == []


def test_fold_layout_folds_the_sequence_a_declared_strand_already_carries() -> None:
    pytest.importorskip("RNA")
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "hp"},
            {"op": "declare_strand", "block": "hp", "sequence": HAIRPIN},
        ],
    )
    _tree, echo = _fold({"strand": "hp"}, tree)
    assert HAIRPIN_MFE in echo
    assert len(tree.domains) == 2


# ── what it refuses, and why ────────────────────────────────────────────


def test_a_bulge_is_refused_by_name_rather_than_laid_out() -> None:
    """``GGGGAGGGGAAAACCCCCCCC`` folds to ``((((.((((....))))))))`` — a
    one-sided bulge, so the two stacks' reverse domains are adjacent with
    ZERO unpaired nucleotides between them. The nominal placement has
    nowhere to put that, and the refusal says which structure it is."""
    rna = pytest.importorskip("RNA")
    sequence = "GGGGAGGGGAAAACCCCCCCC"
    assert chain_fold.mfe_fold(rna, sequence)[0] == "((((.((((....))))))))"
    with pytest.raises(OpError, match="zero unpaired nucleotides"):
        _fold({"strand": "s", "sequence": sequence})


def test_a_nested_two_stack_fold_is_covered_and_lays_out_four_domains() -> None:
    """The covered multi-helix shape: every consecutive domain pair has at
    least one unpaired nucleotide between it."""
    rna = pytest.importorskip("RNA")
    sequence = "GGGGAAAACCCCAAAAGGGGAAAACCCC"
    assert chain_fold.mfe_fold(rna, sequence)[0] == "((((....((((....))))....))))"
    tree, echo = _fold({"strand": "s", "sequence": sequence})
    assert sorted(
        n
        for n, node in tree.blocks.items()
        if (node.chain or {}).get("role") == "helix"
    ) == ["s.h0", "s.h1"]
    route = sorted(tree.domains, key=lambda d: d.ord)
    assert [(d.helix, d.forward, d.loop_before_nt) for d in route] == [
        ("s.h0", True, None),
        ("s.h1", True, 4),
        ("s.h1", False, 4),
        ("s.h0", False, 4),
    ]
    assert "2 helices (8 bp)" in echo
    # The two helices are a helix-spacing apart, as the summary says.
    x0 = (tree.blocks["s.h0"].chain or {})["path"]["waypoints_m"][0][0]
    x1 = (tree.blocks["s.h1"].chain or {})["path"]["waypoints_m"][0][0]
    assert abs((x1 - x0) - nucleic.HELIX_SPACING_M) < 1e-18


def test_an_unpaired_tail_is_refused_because_the_model_cannot_carry_one() -> None:
    pytest.importorskip("RNA")
    with pytest.raises(OpError, match="unpaired at the 5' end"):
        _fold({"strand": "s", "sequence": "AAA" + HAIRPIN})


def test_a_fold_with_no_pairs_at_all_is_refused() -> None:
    pytest.importorskip("RNA")
    with pytest.raises(OpError, match="entirely unpaired"):
        _fold({"strand": "s", "sequence": "AAAAAAAAAAAAAAA"})


def test_an_already_routed_strand_is_refused_rather_than_double_routed() -> None:
    pytest.importorskip("RNA")
    tree, _echo = _fold({"strand": "hp", "sequence": HAIRPIN})
    with pytest.raises(OpError, match="already routes 2 domain"):
        chain_fold.op_fold_layout(
            tree, {"op": "fold_layout", "strand": "hp", "sequence": HAIRPIN}
        )


def test_a_helix_block_is_not_a_fold_target() -> None:
    tree = SeTree()
    apply_ops(tree, _straight_helix_ops("h0", 0.0, 4))
    with pytest.raises(OpError, match="is a 'helix', not a strand"):
        chain_fold.op_fold_layout(
            tree, {"op": "fold_layout", "strand": "h0", "sequence": HAIRPIN}
        )


def test_no_sequence_and_unknown_keys_are_refused_before_any_import() -> None:
    with pytest.raises(OpError, match="has no sequence"):
        _fold({"strand": "hp"})
    with pytest.raises(OpError, match="unknown key"):
        _fold({"strand": "hp", "sequence": HAIRPIN, "iters": 3})
    with pytest.raises(OpError, match="needs strand="):
        _fold({"sequence": HAIRPIN})


def test_scaffold_length_input_is_allowed_but_not_unbounded() -> None:
    with pytest.raises(OpError, match=f"past the {chain_fold.MAX_LAYOUT_NT} nt"):
        _fold({"strand": "hp", "sequence": "A" * (chain_fold.MAX_LAYOUT_NT + 1)})


def test_a_pseudoknotted_dot_bracket_has_no_representation() -> None:
    from precis_se.chain.vocab import ChainError

    with pytest.raises(ChainError, match="never opened"):
        chain_fold.pair_table("(.))")
    with pytest.raises(ChainError, match="leaves 1 pair"):
        chain_fold.pair_table("((.)")


# ── the unavailable branch — runs with or without the library ───────────


def test_fold_layout_raises_unsupported_naming_the_extra(no_rna: None) -> None:
    with pytest.raises(Unsupported, match=r"\[chain\] extra") as caught:
        _fold({"strand": "hp", "sequence": HAIRPIN})
    assert "pip install 'precis-mcp[chain]'" in str(caught.value)


def test_drc_renders_with_exactly_one_fold_unavailable_row(
    handler: SeHandler, no_rna: None
) -> None:
    """The criterion: ``RNA`` unimportable → ``view='drc'`` still renders,
    with ONE ``chain_fold_unavailable`` for the design — not one per
    strand."""
    ops = _straight_helix_ops("h0", 0.0, 8)
    for k, seq in enumerate(("ACGTACGT", "TTTTGGGG")):
        ops += [
            {"op": "add_block", "name": f"s{k}"},
            {"op": "declare_strand", "block": f"s{k}", "sequence": seq},
            {
                "op": "add_domain",
                "strand": f"s{k}",
                "helix": "h0",
                "start": 0,
                "end": 8,
                "forward": k == 0,
            },
        ]
    handler.put(id="unavail", text=json.dumps({"ops": ops}))
    body = handler.get(id="unavail", view="drc").body
    assert body.count("chain_fold_unavailable") == 1
    assert "pip install 'precis-mcp[chain]'" in body
    # The only mention of the check that did not run is inside that row's own
    # detail — no strand got a verdict.
    assert body.count("chain_fold_disagree") == 1


def test_a_design_with_no_sequenced_strand_gets_no_fold_rows(no_rna: None) -> None:
    tree = SeTree()
    apply_ops(tree, _straight_helix_ops("h0", 0.0, 8))
    assert chain_fold.findings(tree) == []


# ── the off-target scan (no library needed) ─────────────────────────────


def _two_staple_design(*, plant: bool) -> SeTree:
    """A 24 nt scaffold with two 12 nt staples. With ``plant`` the second
    staple's 3' half is made the reverse complement of the first's 3' half
    — an 8-nt staple–staple duplex nothing routes."""
    scaffold = "ACCTGAAGTCTGATGCCATGTAGA"
    st0 = _rc(scaffold[0:12])
    st1 = _rc(scaffold[12:24])
    if plant:
        st1 = st1[:4] + _rc(st0[:8])
    ops = _straight_helix_ops("h0", 0.0, 24)
    ops += [
        {"op": "add_block", "name": "sc"},
        {"op": "declare_strand", "block": "sc", "sequence": scaffold},
        {
            "op": "add_domain",
            "strand": "sc",
            "helix": "h0",
            "start": 0,
            "end": 24,
            "forward": True,
        },
    ]
    for name, seq, lo, hi in (("st0", st0, 0, 12), ("st1", st1, 12, 24)):
        ops += [
            {"op": "add_block", "name": name},
            {"op": "declare_strand", "block": name, "sequence": seq},
            {
                "op": "add_domain",
                "strand": name,
                "helix": "h0",
                "start": lo,
                "end": hi,
                "forward": False,
            },
        ]
    tree = SeTree()
    apply_ops(tree, ops)
    return tree


def test_a_planted_eight_nt_staple_staple_complement_fires_chain_offtarget(
    no_rna: None,
) -> None:
    rows = [
        r
        for r in chain_fold.findings(_two_staple_design(plant=True))
        if r.rule == "chain_offtarget"
    ]
    assert len(rows) == 1, [r.subject for r in rows]
    assert "st0" in rows[0].subject and "st1" in rows[0].subject
    assert "8 nt of unintended complementarity" in rows[0].detail


def test_the_declared_scaffold_staple_duplexes_are_not_off_target(
    no_rna: None,
) -> None:
    """Every intended pair is subtracted through
    :func:`precis_se.chain.pairing.derive_pairing`, so a correct design's
    own duplexes never show up — the measurement that makes the planted row
    above mean something."""
    rows = chain_fold.findings(_two_staple_design(plant=False))
    assert _rules(rows) == ["chain_fold_unavailable"]


def test_the_off_target_index_is_linear_and_bounded_in_rows(no_rna: None) -> None:
    """A random 6 kb scaffold has hundreds of chance 8-mers; the report
    carries the longest :data:`MAX_OFFTARGET_ROWS` and then the count."""
    rng = random.Random(7)
    n = 6000
    sequence = "".join(rng.choice("ACGT") for _ in range(n))
    ops = _straight_helix_ops("h0", 0.0, n)
    ops += [
        {"op": "add_block", "name": "sc"},
        {"op": "declare_strand", "block": "sc", "sequence": sequence},
        {
            "op": "add_domain",
            "strand": "sc",
            "helix": "h0",
            "start": 0,
            "end": n,
            "forward": True,
        },
    ]
    tree = SeTree()
    apply_ops(tree, ops)
    rows = [r for r in chain_fold.findings(tree) if r.rule == "chain_offtarget"]
    assert len(rows) == chain_fold.MAX_OFFTARGET_ROWS + 1
    assert rows[-1].subject.startswith("+")
    assert "unintended complementary runs" in rows[-1].detail
    lengths = [int(r.detail.split(" nt of")[0]) for r in rows[:-1]]
    assert lengths == sorted(lengths, reverse=True)
    assert min(lengths) >= chain_fold.OFFTARGET_MIN_NT


# ── the 200 nt fold-check bound ─────────────────────────────────────────


def test_a_six_kb_scaffold_is_skipped_with_its_length_named() -> None:
    pytest.importorskip("RNA")
    rng = random.Random(11)
    n = 6000
    sequence = "".join(rng.choice("ACGT") for _ in range(n))
    ops = _straight_helix_ops("h0", 0.0, n)
    ops += [
        {"op": "add_block", "name": "sc"},
        {"op": "declare_strand", "block": "sc", "sequence": sequence},
        {
            "op": "add_domain",
            "strand": "sc",
            "helix": "h0",
            "start": 0,
            "end": n,
            "forward": True,
        },
    ]
    tree = SeTree()
    apply_ops(tree, ops)
    rows = [r for r in chain_fold.findings(tree) if r.rule == "chain_fold_skipped"]
    assert len(rows) == 1
    assert rows[0].severity == "info"
    assert f"{n} nt is past the {chain_fold.MAX_CHECK_NT} nt" in rows[0].detail
    assert not [r for r in chain_fold.findings(tree) if r.rule == "chain_fold_disagree"]


def test_a_route_that_contradicts_the_mfe_fires_chain_fold_disagree() -> None:
    pytest.importorskip("RNA")
    tree, _echo = _fold({"strand": "hp", "sequence": HAIRPIN})
    # Same route, a sequence that cannot fold into it — the disagreement the
    # rule exists for, reported in sequence offsets.
    (tree.blocks["hp"].chain or {})["sequence"] = "A" * len(HAIRPIN)
    rows = [r for r in chain_fold.findings(tree) if r.rule == "chain_fold_disagree"]
    assert len(rows) == 1
    assert rows[0].severity == "warn"
    assert "4 declared pair(s) the fold does not make" in rows[0].detail
    assert "0·11" in rows[0].detail


# ── handler-level, and out of the pure dry run ──────────────────────────


def test_fold_layout_is_handler_level_and_a_proposal_in_the_web_turn() -> None:
    # Imported in-test: ``precis_web`` needs fastapi, which the host venv
    # skips (the container is where the web turn is exercised).
    from precis_web.design_turn import is_auto_apply, op_vocabulary

    assert "fold_layout" in HANDLER_LEVEL_OPS
    assert "fold_layout" not in known_ops()
    assert "fold_layout" in all_op_names()
    assert not is_auto_apply([{"op": "fold_layout", "strand": "hp"}], kind="se")
    assert "fold_layout{strand" in op_vocabulary("se")


def test_the_pure_dry_run_skips_fold_layout_without_running_it() -> None:
    """The item's rule: neither handler-level chain op runs in
    ``design_turn``'s dry run. Skipping is not the same as passing it to the
    pure table, which would report it as an unknown op — and on a host
    without the ``[chain]`` extra running it would raise instead of
    proposing."""
    from precis_web.design_turn import dry_run_se

    tree = SeTree()
    ops = [{"op": "fold_layout", "strand": "hp", "sequence": HAIRPIN}]
    assert dry_run_se(None, tree, ops, design_slug="d") is None
    assert tree.blocks == {}
    assert tree.domains == []


def test_the_handler_reports_an_op_error_as_bad_input(handler: SeHandler) -> None:
    """``apply.py``'s ``OpError`` → ``BadInput`` path, the same one
    ``relax_chain`` takes."""
    with pytest.raises(BadInput, match="has no sequence"):
        handler.put(
            id="folded",
            text=json.dumps({"ops": [{"op": "fold_layout", "strand": "hp"}]}),
        )
