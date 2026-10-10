"""``refs.last_recalled_at`` — the agent-access stamp the dispatcher writes when
``get`` renders a ref's own view (any kind): throttled, never an edit."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store


def _put(rt: PrecisRuntime, kind: str, text: str) -> int:
    out = rt.dispatch("put", {"kind": kind, "text": text})
    m = re.search(r"id=(\d+)|\b[a-z]{2}(\d+)\b", out)
    assert m, out
    return int(m.group(1) or m.group(2))


def _row(store: Store, ref_id: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_recalled_at, updated_at, last_viewed_at FROM refs "
            "WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _revisions(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM revisions").fetchone()
    assert row is not None
    return int(row[0])


def test_get_stamps_once_per_hour_and_never_touches_updated_at(
    runtime: PrecisRuntime,
    store: Store,
) -> None:
    a = _put(runtime, "memory", "alpha body")
    before = _row(store, a)
    revs = _revisions(store)
    assert before[0] is None

    runtime.dispatch("get", {"kind": "memory", "id": a})
    first = _row(store, a)
    assert isinstance(first[0], datetime)
    # a read is not an edit: updated_at, last_viewed_at (human-only) and the
    # revision log are unchanged
    assert first[1] == before[1] and first[2] is None
    assert _revisions(store) == revs

    runtime.dispatch("get", {"kind": "memory", "id": a, "extent": "fisheye"})
    assert _row(store, a)[0] == first[0]  # throttled: within the hour

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET last_recalled_at = now() - interval '2 hours' "
            "WHERE ref_id = %s",
            (a,),
        )
    stale = _row(store, a)[0]
    runtime.__dict__.pop("_recall_seen", None)  # a fresh process: SQL throttle only
    runtime.dispatch("get", {"kind": "memory", "id": a, "view": "fisheye+1hop"})
    again = _row(store, a)
    assert again[0] > stale and again[1] == before[1]


def test_neighbours_search_hits_and_batches_are_not_recalls(
    runtime: PrecisRuntime,
    store: Store,
) -> None:
    a = _put(runtime, "memory", "alpha unique-marker")
    b = _put(runtime, "memory", "beta body")
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")

    runtime.dispatch("get", {"kind": "memory", "id": a, "extent": "fisheye+1hop"})
    runtime.dispatch("search", {"kind": "memory", "q": "unique-marker"})
    runtime.dispatch("get", {"kind": "memory", "id": [a, b]})
    runtime.dispatch("get", {"kind": "memory", "id": "/recent"})

    assert _row(store, a)[0] is not None
    assert _row(store, b)[0] is None  # a neighbour line is not a read of b


def test_stamp_applies_to_other_kinds(runtime: PrecisRuntime, store: Store) -> None:
    g = _put(runtime, "gripe", "a gripe about something")
    runtime.dispatch("get", {"kind": "gripe", "id": g})
    assert _row(store, g)[0] is not None


def test_a_failing_stamp_never_fails_the_read(
    runtime: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _put(runtime, "memory", "alpha body")

    def boom(self: Store, kind: str, ident: str) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Store, "touch_recalled_for", boom)
    out = runtime.dispatch("get", {"kind": "memory", "id": a})
    assert "alpha body" in out
    assert _row(store, a)[0] is None


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def test_repeat_get_within_the_hour_issues_no_db_statement(
    runtime: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _put(runtime, "memory", "alpha body")
    runtime.dispatch("get", {"kind": "memory", "id": a})
    assert _row(store, a)[0] is not None

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        Store,
        "touch_recalled_for",
        lambda self, kind, ident: calls.append((kind, ident)),
    )
    runtime.dispatch("get", {"kind": "memory", "id": a})
    runtime.dispatch("get", {"kind": "memory", "id": a, "extent": "fisheye"})
    assert calls == []  # absorbed by the in-process TTL cache


def test_file_and_computed_kinds_are_never_looked_up(
    runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        Store,
        "touch_recalled_for",
        lambda self, kind, ident: calls.append((kind, ident)),
    )
    runtime.dispatch("get", {"kind": "skill", "id": "precis-overview"})
    runtime.dispatch("get", {"kind": "markdown", "id": "notes/x.md"})
    assert calls == []


def test_one_statement_stamps_by_slug(runtime: PrecisRuntime, store: Store) -> None:
    p = store.insert_ref(kind="paper", slug="stamp2020slug", title="T")
    runtime.dispatch("get", {"kind": "paper", "id": "stamp2020slug"})
    assert _row(store, p.id)[0] is not None
