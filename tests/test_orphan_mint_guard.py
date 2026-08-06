"""The mint-time half of the orphaned-subtree defect.

``TodoHandler.put`` has rejected a soft-deleted parent since 2026-06-13
(``_todo_guards.check_parent_exists``). The store-layer minting path
deliberately bypasses that handler — and so also bypassed its liveness
check, with nothing replacing it. A review fanout over a draft whose
project todo had been deleted two days earlier minted 258 live
review-todos under the corpse; each was then a dispatch candidate, which
is how a deleted project went on billing planner ticks.

``dispatch._drop_orphaned`` stops such a tree *dispatching*; it does not
stop it *growing*. These tests pin the other direction.
"""

from __future__ import annotations

import pytest

from precis.quest.weave_review import OrphanedParentError, mint_review_todo
from precis.store import Store
from precis.utils.ref_tree import deleted_in_ancestry, is_orphaned


def _todo(store: Store, title: str, *, parent_id: int | None = None) -> int:
    ref = store.insert_ref(
        kind="todo", slug=None, title=title, meta={}, parent_id=parent_id
    )
    return int(ref.id)


def _soft_delete(store: Store, ref_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (ref_id,))
        conn.commit()


def _mint(store: Store, parent_id: int, *, lens: str = "flow") -> int | None:
    return mint_review_todo(
        store,
        parent_id=parent_id,
        lens=lens,
        anchor="dc1",
        text=f"{lens} review",
    )


# ── the mint guard ───────────────────────────────────────────────


def test_mint_rejects_directly_deleted_parent(store: Store) -> None:
    parent = _todo(store, "project")
    _soft_delete(store, parent)

    with pytest.raises(OrphanedParentError):
        _mint(store, parent)


def test_mint_rejects_parent_under_deleted_ancestor(store: Store) -> None:
    """THE regression: ``deleted_at`` is not transitive.

    Deleting the project leaves the section's own ``deleted_at`` NULL, so
    a parent-row-only check waves this through — which is exactly what
    happened to the 258.
    """
    root = _todo(store, "project")
    section = _todo(store, "section", parent_id=root)
    _soft_delete(store, root)

    with pytest.raises(OrphanedParentError):
        _mint(store, section)


def test_mint_writes_nothing_when_it_refuses(store: Store) -> None:
    """The raise must precede the insert — a partial fanout is worse
    than none, since every minted child is its own dispatch candidate."""
    root = _todo(store, "project")
    _soft_delete(store, root)

    with pytest.raises(OrphanedParentError):
        _mint(store, root)

    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s", (root,)
        ).fetchone()
    assert n is not None and int(n[0]) == 0


def test_mint_allows_a_live_tree(store: Store) -> None:
    """Control: the guard must not break the normal path."""
    root = _todo(store, "project")
    section = _todo(store, "section", parent_id=root)

    assert _mint(store, section) is not None


def test_mint_stays_idempotent_on_a_live_tree(store: Store) -> None:
    """The guard runs before the existing-todo check; that check must
    still return ``None`` rather than stacking duplicates."""
    parent = _todo(store, "project")

    assert _mint(store, parent) is not None
    assert _mint(store, parent) is None


# ── the shared ancestry predicate ────────────────────────────────


def test_include_self_distinguishes_the_two_callers(store: Store) -> None:
    """``dispatch`` asks about strict ancestors (its query already
    filtered self); the mint guard asks about self too."""
    dead = _todo(store, "dead")
    _soft_delete(store, dead)

    assert deleted_in_ancestry(store, [dead]) == set()
    assert deleted_in_ancestry(store, [dead], include_self=True) == {dead}


def test_ancestry_walk_reaches_a_deep_ancestor(store: Store) -> None:
    root = _todo(store, "root")
    node = root
    for i in range(5):
        node = _todo(store, f"level {i}", parent_id=node)
    _soft_delete(store, root)

    assert is_orphaned(store, node)


def test_live_tree_is_not_orphaned(store: Store) -> None:
    root = _todo(store, "root")
    kid = _todo(store, "kid", parent_id=root)

    assert not is_orphaned(store, kid)
    assert deleted_in_ancestry(store, [kid], include_self=True) == set()


def test_empty_input_short_circuits(store: Store) -> None:
    assert deleted_in_ancestry(store, []) == set()
