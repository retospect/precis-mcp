"""``passages_contiguous`` — the paper-level quote-contiguity adjacency
helper (``docs/backlog/nanopub-quote-contiguity.md``). DB-backed: the
helper's whole point is a positional-in-the-live-ordering check, which
needs real ``chunks`` rows (including retired / card-variant ones) to
prove."""

from __future__ import annotations

from typing import Any

from precis.nanopub.evidence import passages_contiguous
from tests.workers._helpers import seed_ref


def _insert_chunk(
    store: Any,
    ref_id: int,
    *,
    ord: int,
    text: str = "text",
    retired: bool = False,
) -> int:
    # chunks_check (migration 0001): ord<0 rows must carry a `card_%`
    # chunk_kind (a synthesized card, never body text) — everything else
    # must NOT.
    chunk_kind = "card_glossary" if ord < 0 else "paragraph"
    retired_expr = "now()" if retired else "NULL"
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            f"retired_at) VALUES (%s, 'system', %s, %s, %s, {retired_expr}) "
            "RETURNING chunk_id",
            (ref_id, ord, chunk_kind, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_same_chunk_is_contiguous(store: Any) -> None:
    paper = seed_ref(store, title="one chunk", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    assert passages_contiguous(store, paper, [c0, c0]) is True


def test_adjacent_across_an_ord_gap_is_contiguous(store: Any) -> None:
    # ord 0, 1, 3 with nothing live at ord 2 — positionally adjacent in
    # the live reading order (0,1,3 has no chunk at all in between).
    paper = seed_ref(store, title="ord gap", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    c1 = _insert_chunk(store, paper, ord=1)
    c3 = _insert_chunk(store, paper, ord=3)
    assert passages_contiguous(store, paper, [c0, c1]) is True
    assert passages_contiguous(store, paper, [c1, c3]) is True


def test_intervening_live_chunk_breaks_contiguity(store: Any) -> None:
    paper = seed_ref(store, title="intervening live", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    _c1 = _insert_chunk(store, paper, ord=1)  # live, between c0 and c2
    c2 = _insert_chunk(store, paper, ord=2)
    assert passages_contiguous(store, paper, [c0, c2]) is False


def test_retired_chunk_between_two_quoted_chunks_does_not_break_it(
    store: Any,
) -> None:
    # A retired chunk drops out of the live ordering entirely — the two
    # quoted chunks become positionally adjacent, not separated.
    paper = seed_ref(store, title="retired between", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    _retired = _insert_chunk(store, paper, ord=1, retired=True)
    c2 = _insert_chunk(store, paper, ord=2)
    assert passages_contiguous(store, paper, [c0, c2]) is True


def test_card_variant_excluded_from_the_adjacency_universe(store: Any) -> None:
    # An ord<0 card variant is not part of the paper's text ordering at
    # all — quoting it alongside a live chunk is undecidable, not "adjacent".
    paper = seed_ref(store, title="card variant", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    card = _insert_chunk(store, paper, ord=-1)
    assert passages_contiguous(store, paper, [c0, card]) is False


def test_three_non_adjacent_positions_is_not_contiguous(store: Any) -> None:
    paper = seed_ref(store, title="scattered", kind="paper")
    chunks = [_insert_chunk(store, paper, ord=i) for i in range(6)]
    # positions 0, 2, 5 — not one adjacent run.
    picked = [chunks[0], chunks[2], chunks[5]]
    assert passages_contiguous(store, paper, picked) is False


def test_empty_and_singleton_are_trivially_contiguous(store: Any) -> None:
    paper = seed_ref(store, title="trivial", kind="paper")
    c0 = _insert_chunk(store, paper, ord=0)
    assert passages_contiguous(store, paper, []) is True
    assert passages_contiguous(store, paper, [c0]) is True
