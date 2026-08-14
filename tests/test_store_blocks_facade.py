"""Pins the ``store.blocks`` (:class:`BlockStore`) carve finished in
``docs/backlog/codereview-store-decomposition.md``.

Not a test of block *semantics* (``test_blocks.py`` owns that) — just
that the carve holds its final shape: blocks is composed, not mixed in,
and the transitional flat delegations on ``Store`` stay deleted.
"""

from __future__ import annotations

import inspect

from precis.store._blocks_ops import BlockStore
from precis.store.store import Store
from precis.store.types import BlockInsert


def _public_callables(cls: type) -> set[str]:
    """Public, non-underscore, non-``object``-inherited callables on
    ``cls`` — the ``BlockStore`` domain surface."""
    names: set[str] = set()
    for name in dir(cls):
        if name.startswith("_") or name in ("pool", "tx"):
            continue
        if hasattr(object, name):
            continue
        member = inspect.getattr_static(cls, name)
        if isinstance(member, property):
            continue
        if not callable(getattr(cls, name)):
            continue
        names.add(name)
    return names


def test_store_no_longer_mixes_blocks_into_its_own_mro() -> None:
    """The carve: ``blocks`` is composed (``store.blocks``), not mixed
    into ``Store``'s bases — no blocks mixin may reappear in
    ``Store.__mro__``."""
    mro_names = {c.__name__ for c in Store.__mro__}
    assert "BlocksMixin" not in mro_names
    assert "BlockStore" not in mro_names


def test_no_flat_block_delegations_remain_on_store() -> None:
    """Block ops are reached only as ``store.blocks.*``: no public
    ``BlockStore`` method may (re)appear under its flat historical name on
    ``Store`` — a same-named method sneaking back in would silently shadow
    the sub-store and resurrect the flat-namespace collision class the
    decomposition exists to kill."""
    blockstore_methods = _public_callables(BlockStore)
    assert len(blockstore_methods) > 30, (
        "sanity check — expected dozens of BlockStore methods, got "
        f"{len(blockstore_methods)}; did the reflection break?"
    )
    leaked = sorted(n for n in blockstore_methods if hasattr(Store, n))
    assert not leaked, f"flat block methods reappeared on Store: {leaked}"


def test_blocks_substore_reads_its_own_writes(store: Store) -> None:
    """A tiny live smoke test: ``store.blocks`` writes and reads through
    the shared :class:`~precis.store.core.StoreCore` (same pool as the
    host)."""
    ref = store.insert_ref(
        kind="plaintext",
        slug="blocks-facade-smoke-ref",
        title="blocks-facade-smoke-ref",
    )
    [block] = store.blocks.insert_blocks(
        ref.id, [BlockInsert(pos=0, text="written through store.blocks")]
    )
    got = store.blocks.get_block(ref.id, pos=block.pos)
    assert got is not None
    assert got.text == "written through store.blocks"
