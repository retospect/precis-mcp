"""OracleHandler — ``_render_index`` trailer hints.

No dedicated oracle test module existed before this (oracle coverage was
scattered across ``test_oracle_lens.py`` / ``test_ingest_oracles.py`` /
crosslink tests) — this one is scoped to the ``Next:`` trailer fix on
``_render_index``: the record handle used to be shadowed by the
per-block loop variable, so the "random entry (default)" hint
advertised the LAST catalog entry's chunk handle instead of the ref's
own handle.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.oracle import OracleHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry
from tests.hintcheck import assert_hints_round_trip


def _seed_oracle(store: Store, slug: str, n_entries: int) -> int:
    """Insert an oracle ref with ``n_entries`` 1-indexed blocks (no
    embeddings needed — the index view doesn't touch search)."""
    ref = store.insert_ref(kind="oracle", slug=slug, title=slug.title())
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                slug=None,
                text=f"entry {i} body",
                token_count=2,
                embedding=None,
                density="sparse",
                meta={"section_path": [f"Entry {i}"]},
            )
            for i in range(1, n_entries + 1)
        ],
    )
    return ref.id


def test_render_index_hints_round_trip_and_use_ref_handle(
    store: Store, hub: Hub
) -> None:
    """Every hint on the ``/index`` catalog page must parse + dispatch,
    and the "random entry (default)" hint specifically must advertise
    the REF's own handle — not the last block's chunk handle (the
    loop-variable-shadowing bug: ``handle`` was rebound by the
    ``for block in blocks`` loop before this fix)."""
    ref_id = _seed_oracle(store, "test-oracle-idx", 3)
    handler = OracleHandler(hub=hub)

    resp = handler.get(id="test-oracle-idx/index")

    def dispatch(verb: str, kwargs: dict) -> object:
        kwargs.pop("kind", None)
        return getattr(handler, verb)(**kwargs)

    hints = assert_hints_round_trip(resp.body, dispatch)

    ref_handle = handle_registry.format_handle("oracle", ref_id)
    # "random entry (default)" — must be the REF handle, not a chunk one.
    assert f"get(id={ref_handle!r})" in hints
    # The deterministic-entry hint interpolates a REAL 1-indexed
    # position (entries printed on the page), not a ``~N`` placeholder.
    assert any(
        h.startswith("get(kind='oracle', id='test-oracle-idx~") and "N" not in h
        for h in hints
    )


def test_render_index_default_hint_not_last_entrys_chunk_handle(
    store: Store, hub: Hub
) -> None:
    """Regression guard for the specific shadowing bug: the "random
    entry" hint must be the ref handle, not the LAST entry's own
    per-block chunk handle (what the shadowed ``handle`` variable held
    right before the bugfix)."""
    ref_id = _seed_oracle(store, "test-oracle-idx-2", 5)
    handler = OracleHandler(hub=hub)
    resp = handler.get(id="test-oracle-idx-2/index")
    ref_handle = handle_registry.format_handle("oracle", ref_id)

    last_block = store.chunks.get_chunk(ref_id, pos=5)
    assert last_block is not None
    last_block_handle = (
        handle_registry.try_format("oracle", last_block.id, chunk=True)
        or "test-oracle-idx-2~5"
    )

    default_line = next(
        line for line in resp.body.splitlines() if "random entry (default)" in line
    )
    assert f"get(id={ref_handle!r})" in default_line
    assert last_block_handle not in default_line
