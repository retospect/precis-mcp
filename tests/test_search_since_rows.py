"""gr592952 slice 4: a date-windowed source search dates its rows.

``search`` with ``since=``/``until=`` routes to the cross-kind source
primitive; every returned row's header then ends with the ref's created
date, so the caller can verify the window per row instead of trusting an
unlabeled filter.
"""

from __future__ import annotations

import re

from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store

_ROW_RE = re.compile(r"^## \d+\. .*$", re.MULTILINE)
_CREATED_RE = re.compile(r" · created (\d{4}-\d{2}-\d{2})$")


def _seed_at(
    rt: PrecisRuntime, kind: str, slug: str, title: str, text: str, created: str
) -> int:
    store = rt.hub.store
    assert store is not None
    ref = store.insert_ref(kind=kind, slug=slug, title=title)
    emb = rt.hub.embedder
    vec = emb.embed_one(text) if emb is not None else None
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text, embedding=vec)])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (created, ref.id)
        )
        conn.commit()
    return ref.id


def _row_headers(body: str) -> list[str]:
    rows = _ROW_RE.findall(body)
    assert rows, f"no search rows rendered: {body!r}"
    return rows


def test_since_rows_carry_created_date_in_window(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    _seed_at(
        rt,
        "paper",
        "win-a",
        "Alpha study",
        "spintronic magnon transport in insulators",
        "2026-11-10T12:00:00Z",
    )
    _seed_at(
        rt,
        "web",
        "win-b",
        "Beta note",
        "spintronic magnon transport review",
        "2026-11-12T08:30:00Z",
    )

    out = rt.dispatch(
        "search",
        {
            "kind": "paper,web",
            "q": "spintronic magnon transport",
            "since": "2026-11-01T00:00:00Z",
            "until": "2026-11-16T00:00:00Z",
        },
    )
    rows = _row_headers(out)
    assert len(rows) == 2
    for row in rows:
        m = _CREATED_RE.search(row)
        assert m is not None, f"row has no created date: {row!r}"
        assert "2026-11-01" <= m.group(1) <= "2026-11-16"


def test_per_kind_search_rows_are_dated(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    _seed_at(
        rt,
        "paper",
        "pk-a",
        "Gamma study",
        "topological insulator surface states",
        "2026-11-08T00:00:00Z",
    )

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "topological insulator",
            "since": "2026-11-01T00:00:00Z",
        },
    )
    rows = _row_headers(out)
    assert any(_CREATED_RE.search(r) for r in rows)


def test_undated_search_rows_unchanged(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    _seed_at(
        rt,
        "paper",
        "bare-a",
        "Delta study",
        "quantum spin liquid frustration",
        "2026-11-08T00:00:00Z",
    )

    out = rt.dispatch(
        "search", {"kind": "paper", "q": "quantum spin liquid", "sort": "recency"}
    )
    for row in _row_headers(out):
        assert " · created " not in row


def test_row_date_matches_the_refs_actual_created_day(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    _seed_at(
        rt,
        "paper",
        "exact-a",
        "Epsilon study",
        "molten salt reactor corrosion loop",
        "2026-11-05T23:45:00Z",
    )
    store: Store | None = rt.hub.store
    assert store is not None
    refs = store.list_refs(kind=["paper"], tags=None, limit=10)
    day = next(r.created_at.date().isoformat() for r in refs if "Epsilon" in r.title)

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "molten salt corrosion",
            "since": "2026-11-01T00:00:00Z",
        },
    )
    rows = _row_headers(out)
    assert len(rows) == 1
    m = _CREATED_RE.search(rows[0])
    assert m is not None and m.group(1) == day
