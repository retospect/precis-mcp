"""``design_turn.dry_run_se`` against the handler-level roster
(:data:`precis_se.atomic.apply.HANDLER_LEVEL_OPS`).

The dry run decides whether a model reply becomes a proposal at all: a
handler-level op it neither prepares nor deliberately skips falls through
to the pure table, which reports it as an unknown op — so the user sees a
``dry_run`` error instead of a proposal. ``join`` drifted that way once;
these tests pin the roster so the next drift fails here, named, rather
than in the workbench.
"""

from __future__ import annotations

from precis.store import Store
from precis_se.atomic.apply import HANDLER_LEVEL_OPS
from precis_se.atomic.generate import finish_generate, prepare_generate
from precis_se.ops import SeTree
from precis_web.design_turn import (
    _SE_DRY_RUN_PREPARE,
    _SE_DRY_RUN_SKIPPED,
    dry_run_se,
)

_TUBE_Z8 = "hexfold 0.2\na: tube(8,0, len=3)\n"


def test_every_handler_level_op_is_prepared_or_deliberately_skipped() -> None:
    """Adding a name to ``HANDLER_LEVEL_OPS`` without touching
    ``dry_run_se``'s two tables fails here, naming the op."""
    roster = set(HANDLER_LEVEL_OPS)
    unhandled = roster - set(_SE_DRY_RUN_PREPARE) - _SE_DRY_RUN_SKIPPED
    assert not unhandled, (
        f"handler-level op(s) {sorted(unhandled)} are neither prepared nor "
        "skipped by precis_web.design_turn.dry_run_se — the pure table would "
        "report them as unknown ops in every workbench proposal"
    )
    # Both tables name only handler-level ops, and never the same one twice.
    assert set(_SE_DRY_RUN_PREPARE) <= roster
    assert _SE_DRY_RUN_SKIPPED.issubset(roster)
    assert not set(_SE_DRY_RUN_PREPARE) & _SE_DRY_RUN_SKIPPED


def _generate(store: Store, tree: SeTree, name: str, design_slug: str) -> None:
    _echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE_Z8},
            "name": name,
        },
        design_slug,
    )
    assert pending is not None
    finish_generate(store, tree, pending)


def test_join_dry_runs_clean_and_mints_nothing(store: Store) -> None:
    tree = SeTree()
    _generate(store, tree, "tube_a", "dry-join")
    _generate(store, tree, "tube_b", "dry-join")
    before = set(tree.blocks)

    op = {"op": "join", "name": "composite", "a": "tube_a.out", "b": "tube_b.in"}
    assert dry_run_se(store, tree, [op], design_slug="dry-join") is None
    # The prepare half ran on the scratch copy only: no composite on the
    # caller's tree, and no composite structure minted in the store.
    assert set(tree.blocks) == before
    assert store.get_ref(kind="structure", id="dry-join-composite") is None


def test_join_dry_run_reports_joins_own_error_not_unknown_op(store: Store) -> None:
    tree = SeTree()
    _generate(store, tree, "tube_a", "dry-join-bad")
    op = {"op": "join", "name": "c", "a": "tube_a.out", "b": "nope.in"}
    error = dry_run_se(store, tree, [op], design_slug="dry-join-bad")
    assert error is not None
    assert "unknown op" not in error
    assert "no such block: 'nope'" in error
