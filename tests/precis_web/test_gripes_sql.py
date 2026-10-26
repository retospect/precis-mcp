"""Real-PG companion for ``routes/gripes.py``'s raw SQL.

The route-level tests in ``test_gripe_count_links.py`` drive ``/gripes``
and ``/gripes/rows`` through a TestClient; this file is the
convention-required ``test_<module>_sql.py`` companion
(``tests/test_convention_raw_sql_companion.py`` — FakeStore never parses
SQL) that calls ``_status_counts`` / ``_rows`` directly on the live
``store`` fixture: the grouped chip count, the open-rows load, the
cohort filters, the off-vocabulary drift round-trip the chips rely on,
and the lazy fragment's ``LIMIT page+1``/``OFFSET`` paging under tied
``updated_at`` plus the ``/gripes/rows`` status allowlist.
"""

from __future__ import annotations

from typing import Any

from precis.store import Tag
from precis_web.routes.gripes import (
    _ROWS_PAGE,
    _STATUS_PARAM_VALUES,
    _rows,
    _status_counts,
)
from tests._refs import insert_gripe


def _seed_cohort(store: Any, status: str, n: int, prefix: str) -> list[int]:
    """``n`` gripes under one STATUS in a single transaction — the
    migration-0176 commit-time trigger wants ref + tag atomic, and one
    tx keeps a 100-row cohort cheap."""
    ids: list[int] = []
    with store.tx() as conn:
        for i in range(n):
            ref = store.insert_ref(
                kind="gripe",
                slug=None,
                title=f"{prefix}-{i:04}",
                meta={},
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", status),
                set_by="agent",
                conn=conn,
            )
            ids.append(int(ref.id))
    return ids


def _seed_off_vocab_gripe(store: Any, title: str, status: str) -> int:
    """A gripe carrying an off-vocabulary STATUS — pre-0176 drift the
    commit-time trigger now refuses, simulated with triggers bypassed
    (same ``session_replication_role`` trick ``test_gripe_count_links``
    uses)."""
    with store.tx() as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


def test_status_counts_groups_live_and_retired_buckets(store: Any) -> None:
    """One row per effective status: live gripes bucket under their
    STATUS value, a soft-deleted gripe buckets under the ``retired``
    pseudo-status (not its last STATUS), and an off-vocabulary drift
    value surfaces rather than hiding its rows."""
    insert_gripe(store, "open-a")
    insert_gripe(store, "open-b")
    insert_gripe(store, "triaged", status="triaged")
    for i in range(3):
        insert_gripe(store, f"done-{i}", status="done")
    retired = insert_gripe(store, "gone", status="wontfix")
    store.retire_ref(retired.id)
    _seed_off_vocab_gripe(store, "legacy-closed", "closed")

    chips = _status_counts(store)

    assert {c["status"]: c["count"] for c in chips} == {
        "open": 2,
        "triaged": 1,
        "done": 3,
        "closed": 1,
        "retired": 1,
    }
    # Workflow-rank order first; off-vocabulary values sort
    # alphabetically after the known ranks.
    assert [c["status"] for c in chips] == [
        "open",
        "triaged",
        "done",
        "closed",
        "retired",
    ]


def test_rows_open_loads_only_live_open_with_prio(store: Any) -> None:
    """The ``GET /gripes`` open load: ``status_filter='open'`` returns
    the live, untagged-as-anything-else cohort — other statuses and
    retired rows stay out, and the correlated PRIO subquery resolves."""
    a = insert_gripe(store, "open-a")
    store.add_tag(a.id, Tag.closed("PRIO", "high"), set_by="agent")
    b = insert_gripe(store, "open-b")
    insert_gripe(store, "triaged", status="triaged")
    gone = insert_gripe(store, "gone")
    store.retire_ref(gone.id)

    rows = _rows(store, status_filter="open")

    by_id = {r["ref_id"]: r for r in rows}
    assert set(by_id) == {int(a.id), int(b.id)}
    assert all(r["status"] == "open" for r in rows)
    assert by_id[int(a.id)]["prio"] == "high"
    assert by_id[int(b.id)]["prio"] is None


def test_rows_status_filters(store: Any) -> None:
    """The ``live``/``all``/``retired`` cohort filters and a single-status
    bind each return exactly their own rows — ``live`` is everything but
    the terminal values, ``all`` includes the retired bucket."""
    o = insert_gripe(store, "live-open")
    d = insert_gripe(store, "done", status="done")
    w = insert_gripe(store, "wontfix", status="wontfix")
    ret = insert_gripe(store, "retired", status="done")
    store.retire_ref(ret.id)

    live_ids = {r["ref_id"] for r in _rows(store, status_filter="live")}
    all_ids = {r["ref_id"] for r in _rows(store, status_filter="all")}
    ret_ids = {r["ref_id"] for r in _rows(store, status_filter="retired")}
    done_ids = {r["ref_id"] for r in _rows(store, status_filter="done")}

    assert live_ids == {int(o.id)}
    assert all_ids == {int(o.id), int(d.id), int(w.id), int(ret.id)}
    assert ret_ids == {int(ret.id)}
    assert done_ids == {int(d.id)}


def test_rows_serves_the_drift_status_the_count_surfaces(store: Any) -> None:
    """The chips pass whatever the grouped count reported, so an
    off-vocabulary drift status round-trips: ``_rows(status_filter=
    'closed')`` binds the value and serves the drifted gripe rather
    than erroring or hiding it."""
    gid = _seed_off_vocab_gripe(store, "legacy-closed", "closed")

    rows = _rows(store, status_filter="closed")

    assert [r["ref_id"] for r in rows] == [gid]
    assert rows[0]["status"] == "closed"


def test_rows_offset_paging_distinct_ids_under_tied_updated_at(
    store: Any,
) -> None:
    """The lazy fragment's offset paging must not repeat or drop rows
    when ``updated_at`` ties — the ``ref_id DESC`` tiebreaker is what
    keeps pages disjoint. Seven ``done`` gripes on one timestamp page
    3-at-a-time to 7 distinct ids in ref_id-descending order."""
    ids = _seed_cohort(store, "done", 7, "tied")
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = '2026-01-01 00:00:00+00' WHERE kind = 'gripe'"
        )

    pages = [
        _rows(store, status_filter="done", limit=3, offset=off) for off in (0, 3, 6)
    ]
    got = [r["ref_id"] for page in pages for r in page]

    assert len(got) == len(set(got)) == 7
    assert set(got) == set(ids)
    assert got == sorted(ids, reverse=True)


def test_rows_fetches_page_plus_one_to_detect_the_cap(store: Any) -> None:
    """The ``limit=_ROWS_PAGE + 1`` probe the routes use: one row past
    the cap means a next page exists. A ``_ROWS_PAGE + 2`` cohort serves
    ``_ROWS_PAGE + 1``, then the remainder on the next offset."""
    ids = _seed_cohort(store, "done", _ROWS_PAGE + 2, "cap")

    first = _rows(store, status_filter="done", limit=_ROWS_PAGE + 1, offset=0)
    assert len(first) == _ROWS_PAGE + 1  # the +1 is the next-page probe

    second = _rows(store, status_filter="done", limit=_ROWS_PAGE + 1, offset=_ROWS_PAGE)
    assert len(second) == 2

    # The routes render rows[:_ROWS_PAGE]; sliced that way the pages are
    # disjoint and cover the cohort — the probe row overlaps page 2's
    # head by design (offset continues from _ROWS_PAGE, not +1).
    got = [r["ref_id"] for r in (*first[:_ROWS_PAGE], *second[:_ROWS_PAGE])]
    assert len(got) == len(set(got)) == _ROWS_PAGE + 2
    assert set(got) == set(ids)


def test_rows_allowlist_unions_drift_statuses_from_the_count(
    store: Any,
) -> None:
    """``/gripes/rows`` allowlists ``status`` to ``_STATUS_PARAM_VALUES``
    plus whatever the grouped count returns — a drift status's chip
    stays expandable, and its rows fragment serves the drifted gripes.
    A value in neither is refused upstream (the 400 path the route
    tests cover)."""
    _seed_off_vocab_gripe(store, "legacy-closed", "closed")

    # The same union expression status_rows() builds its allowlist from.
    allowed = tuple(
        dict.fromkeys(
            (
                *_STATUS_PARAM_VALUES,
                *(str(c["status"]) for c in _status_counts(store)),
            )
        )
    )
    assert "closed" in allowed
    assert "bogus" not in allowed
