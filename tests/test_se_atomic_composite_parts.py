"""``composite_part_stolen`` — a join composite whose recorded ``parts``
are no longer its own children (docs/backlog/se-composite-integrity.md).

The invariant is one comparison per recorded part, and the whole value of
the check is in what it does *not* fire on: an ordinary chained join,
where a composite legitimately becomes a part of an outer composite, is
the common case and must stay silent. Both prod dogfood designs reached
the violating shape and a green suite, a prod dogfood and a follow-up
audit all missed it, so every branch is pinned here.
"""

from __future__ import annotations

from typing import Any

from precis_se.atomic import validate as se_atomic_validate
from precis_se.ops import SeBlock, SeTree


def _block(
    name: str, *, parent: str | None = None, bound: str | None = None
) -> SeBlock:
    return SeBlock(
        name=name,
        envelope="sphere:r2e-10",
        parent=parent,
        bound_kind="structure" if bound else None,
        bound=bound,
    )


def _join_record(*parts: tuple[str, str]) -> dict[str, Any]:
    return {
        "generator": "join",
        "parts": [{"block": block, "structure": slug} for block, slug in parts],
    }


def _two_tube_join() -> SeTree:
    """The ordinary shape: ``composite`` claims ``tube_a``/``tube_b``, and
    both are its children."""
    tree = SeTree()
    tree.blocks["composite"] = _block("composite", bound="d-composite")
    tree.blocks["tube_a"] = _block("tube_a", parent="composite", bound="d-tube_a")
    tree.blocks["tube_b"] = _block("tube_b", parent="composite", bound="d-tube_b")
    return tree


def test_a_well_formed_join_composite_produces_no_finding() -> None:
    findings = se_atomic_validate.validate_atomic(
        _two_tube_join(),
        generated_records={
            "d-composite": _join_record(("tube_a", "d-tube_a"), ("tube_b", "d-tube_b"))
        },
    )
    assert [f for f in findings if f.rule == "composite_part_stolen"] == []


def test_a_part_reparented_onto_a_second_composite_is_an_error() -> None:
    tree = _two_tube_join()
    # What the 2026-09-30 prod design actually looks like: a later join
    # took tube_b, and `composite` still claims it.
    tree.blocks["composite2"] = _block("composite2", bound="d-composite2")
    tree.blocks["tube_b"].parent = "composite2"
    findings = se_atomic_validate.validate_atomic(
        tree,
        generated_records={
            "d-composite": _join_record(("tube_a", "d-tube_a"), ("tube_b", "d-tube_b")),
            "d-composite2": _join_record(("tube_b", "d-tube_b")),
        },
    )
    (finding,) = [f for f in findings if f.rule == "composite_part_stolen"]
    assert finding.severity == "error"
    assert finding.subject == "composite"
    # Both ends named — the remedy depends on which composite keeps it.
    assert "'tube_b'" in finding.detail and "'composite2'" in finding.detail
    assert "tube_b_* ports" in finding.detail


def test_a_chained_join_is_legitimate_and_draws_nothing() -> None:
    """``composite`` joined onto a further tube: it becomes a part of
    ``outer`` while keeping its own parts. Only the outer record gains an
    entry, so nothing is stolen."""
    tree = _two_tube_join()
    tree.blocks["outer"] = _block("outer", bound="d-outer")
    tree.blocks["composite"].parent = "outer"
    tree.blocks["tube_c"] = _block("tube_c", parent="outer", bound="d-tube_c")
    findings = se_atomic_validate.validate_atomic(
        tree,
        generated_records={
            "d-composite": _join_record(("tube_a", "d-tube_a"), ("tube_b", "d-tube_b")),
            "d-outer": _join_record(
                ("composite", "d-composite"), ("tube_c", "d-tube_c")
            ),
        },
    )
    assert [f for f in findings if f.rule == "composite_part_stolen"] == []


def test_a_part_removed_from_the_design_is_an_error_naming_the_record() -> None:
    tree = _two_tube_join()
    del tree.blocks["tube_b"]
    findings = se_atomic_validate.validate_atomic(
        tree,
        generated_records={
            "d-composite": _join_record(("tube_a", "d-tube_a"), ("tube_b", "d-tube_b"))
        },
    )
    (finding,) = [f for f in findings if f.rule == "composite_part_stolen"]
    assert finding.severity == "error"
    assert "no block of that name" in finding.detail
    assert "'d-tube_b'" in finding.detail


def test_a_non_join_generator_record_is_never_subject_to_the_check() -> None:
    """The discriminator is ``generator == 'join'``, the same one
    :func:`precis_se.atomic.join._join_generated_record` uses — a
    ``hexfold`` record that happened to carry a ``parts`` key is not a
    composite."""
    tree = _two_tube_join()
    tree.blocks["tube_b"].parent = None
    findings = se_atomic_validate.validate_atomic(
        tree,
        generated_records={
            "d-composite": {
                "generator": "hexfold",
                "parts": [{"block": "tube_b", "structure": "d-tube_b"}],
            }
        },
    )
    assert [f for f in findings if f.rule == "composite_part_stolen"] == []


def test_a_caller_that_hydrates_no_records_skips_the_check_rather_than_raising() -> (
    None
):
    """``propose`` calls ``validate_atomic`` with no store access, so the
    check has to degrade to silence — not to a false finding and not to a
    KeyError."""
    tree = _two_tube_join()
    tree.blocks["tube_b"].parent = "composite2"
    assert se_atomic_validate.validate_atomic(tree) == []
