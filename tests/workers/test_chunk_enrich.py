"""Chunk-context enrichment for the chunk-level classifiers (gr690600).

``classify._enrich`` and ``axis_pass._enrich_chunk`` fill each claimed
row's ``title``, ``position``, ``section_path``, ``prev_gist`` and
``next_gist``. DB-backed via the ``store`` fixture: pins the filled values
and that the statement count does not grow with the batch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from precis.workers.axis_pass import _enrich_chunk
from precis.workers.classify import _enrich
from tests.factories import insert_gist
from tests.workers._helpers import seed_chunk, seed_ref

_ENRICHERS = pytest.mark.parametrize(
    "enrich", [_enrich, _enrich_chunk], ids=["classify", "axis_pass"]
)

_LONG = "  first line\nsecond line " + "x" * 300 + "  "


class _CountingConn:
    """Forwards ``execute`` to a real connection and counts the calls."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.statements = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.statements += 1
        return self._conn.execute(*args, **kwargs)


def _seed(store: Any) -> list[dict[str, Any]]:
    """Two papers; returns one claim-shaped row per body chunk.

    Paper A: a card at ord -1 and body ords 0-3; ord 1 carries an ok gist,
    ord 2 a failed one, ord 3 an empty ok one. Paper B: body ords 0, 1 and
    3 (a gap at 2), the ord-1 text needing whitespace clean-up and the cut.
    """
    a = seed_ref(store, title="Paper A")
    b = seed_ref(store, title="Paper B")
    seed_chunk(store, ref_id=a, ord=-1, chunk_kind="card_abstract", text="A card")
    body = [
        (a, 0, "A zero", ["Intro", "Background"]),
        (a, 1, "A one", []),
        (a, 2, "A two", ["Methods"]),
        (a, 3, "A three", []),
        (b, 0, "B zero", []),
        (b, 1, _LONG, []),
        (b, 3, "B three", []),
    ]
    rows: list[dict[str, Any]] = [
        {
            "chunk_id": seed_chunk(store, ref_id=ref_id, ord=ord_, text=text),
            "ref_id": ref_id,
            "ord": ord_,
            "text": text,
            "section_path": path,
        }
        for ref_id, ord_, text, path in body
    ]
    insert_gist(store, rows[1]["chunk_id"], "gist of A one")
    insert_gist(store, rows[3]["chunk_id"], "")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, 'llm-v1', 'failed gist of A two', 'failed')",
            (rows[2]["chunk_id"],),
        )
        conn.commit()
    return rows


def _copies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "section_path": list(r["section_path"])} for r in rows]


def _statements(
    store: Any, enrich: Callable[[Any, list[dict[str, Any]]], None], rows: list[dict]
) -> int:
    with store.pool.connection() as conn:
        counting = _CountingConn(conn)
        enrich(counting, rows)
        return counting.statements


@_ENRICHERS
def test_enrich_fills_title_position_section_and_neighbour_gists(
    store: Any, enrich: Callable[[Any, list[dict[str, Any]]], None]
) -> None:
    rows = _copies(_seed(store))
    with store.pool.connection() as conn:
        enrich(conn, rows)

    got = [
        (
            r["title"],
            r["position"],
            r["section_path"],
            r["prev_gist"],
            r["next_gist"],
        )
        for r in rows
    ]
    cleaned = _LONG.strip().replace("\n", " ")[:160]
    assert len(cleaned) == 160 and cleaned.startswith("first line second line x")
    assert got == [
        # ord 0: the card at ord -1 is not a previous chunk; the ok gist
        # of ord 1 wins over its raw text.
        ("Paper A", "0/4", "Intro ▸ Background", "", "gist of A one"),
        # A failed gist is ignored: raw text.
        ("Paper A", "1/4", "(none)", "A zero", "A two"),
        # An empty ok gist falls back to raw text.
        ("Paper A", "2/4", "Methods", "gist of A one", "A three"),
        ("Paper A", "3/4", "(none)", "A two", ""),
        ("Paper B", "0/3", "(none)", "", cleaned),
        # ord 2 does not exist: no next, and ord 3 has no previous.
        ("Paper B", "1/3", "(none)", "B zero", ""),
        ("Paper B", "3/3", "(none)", "", ""),
    ]


@_ENRICHERS
def test_enrich_statement_count_does_not_grow_with_the_batch(
    store: Any, enrich: Callable[[Any, list[dict[str, Any]]], None]
) -> None:
    rows = _seed(store)
    one = _statements(store, enrich, _copies(rows[1:2]))
    batch = _statements(store, enrich, _copies(rows))
    assert batch == one, f"{batch} statements for {len(rows)} rows, {one} for one"


@_ENRICHERS
def test_enrich_empty_batch_issues_no_statement(
    store: Any, enrich: Callable[[Any, list[dict[str, Any]]], None]
) -> None:
    assert _statements(store, enrich, []) == 0
