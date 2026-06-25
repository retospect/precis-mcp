"""TOC handles are universal handles that round-trip through ``get``.

Regression for the ADR-0036 gap: ``view='toc'`` used to label each
cluster with the ``kind:slug~pos`` legacy form (``paper:vaswani17~0..8``),
which the ``get`` id parser rejects on ``:`` — so a copy-pasted drill-in
hint dead-ended. The renderer now emits the record's universal handle
(``pa<id>~lo..hi``); these tests drive a real store + ``PaperHandler``
end-to-end and feed an emitted handle straight back into ``get``.
"""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store
from precis.utils import handle_registry


def _seed_paper(store: Store, *, slug: str, n: int) -> int:
    """Insert a paper with ``n`` body chunks and two keyword topics.

    The keyword halves give ``segment_dp`` a real boundary, so the
    bucketed path emits multi-chunk ``~lo..hi`` ranges (not just the
    per-chunk fallback). Returns the paper ``ref_id``.
    """
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    blocks = store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=f"chunk {i} body text", embedding=e.embed_one("x"))
            for i in range(n)
        ],
    )
    half = n // 2
    # Set keywords directly (the chunk_keywords worker's column) so the
    # clusterer has signal — first half topic A, second half topic B.
    with store.pool.connection() as conn:
        for i, b in enumerate(blocks):
            kws = ["alpha", "beta", "shared"] if i < half else ["gamma", "delta", "shared"]
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE chunk_id = %s", (kws, b.id)
            )
    return ref.id


def test_toc_handles_use_universal_handle_and_round_trip(store: Store) -> None:
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store, slug="vaswani17", n=70)
    pa = handle_registry.format_handle("paper", ref_id)  # e.g. "pa123"

    # Full TOC: headline + every row handle is the universal handle, and
    # the legacy kind:slug form is gone.
    out = handler.get(id=pa, view="toc").body
    assert out.startswith(f"# {pa} TOC")
    assert "paper:vaswani17" not in out
    assert "vaswani17~" not in out

    # Pull a multi-chunk handle out of the rendered table and feed it back.
    ranges = re.findall(rf"{re.escape(pa)}~(\d+)\.\.(\d+)", out)
    assert ranges, f"expected a ~lo..hi handle in TOC output:\n{out}"
    lo, hi = ranges[0]
    sub_handle = f"{pa}~{lo}..{hi}"

    # Round-trip 1: scoped TOC drill-down via the emitted handle.
    sub = handler.get(id=sub_handle, view="toc").body
    assert f"sub-TOC ~{lo}..{hi}" in sub

    # Round-trip 2: the same handle without a view reads the chunk range.
    chunks = handler.get(id=sub_handle).body
    assert chunks  # no BadInput / NotFound raised


def test_drill_in_hint_is_a_valid_get_id(store: Store) -> None:
    """The ``Next: drill into fat clusters`` hint must parse as a get id."""
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store, slug="attention", n=80)
    pa = handle_registry.format_handle("paper", ref_id)

    out = handler.get(id=pa, view="toc").body
    hint_ids = re.findall(r"get\(kind='paper', id='([^']+)', view='toc'\)", out)
    assert hint_ids, f"expected at least one drill-in hint:\n{out}"
    for hid in hint_ids:
        assert hid.startswith(f"{pa}~")
        # Each hint id resolves without raising.
        handler.get(id=hid, view="toc")
    # The superfluous "# N chunks" comment is gone from the hint lines.
    for line in out.splitlines():
        if "view='toc'" in line and "get(" in line:
            assert "chunks" not in line
