"""gr592737: "gripes closed between A and B" — closure-window filtering.

``closed_since``/``closed_until`` bound the *closure* clock — the
set-time (``ref_tags.created_at``) of the ref's newest live ``STATUS``
tag in the closed set (default ``done``/``wontfix``) — not the ref's
mint time that ``since``/``until`` read. The filter lands on
``Store.list_refs``/``count_refs``/``count_refs_facets`` (slice 1), the
queryless listing + ``view='count'`` handler paths (slices 2 + 5), the
``status='resolved'`` alias and ``id='/done'`` list view (slice 3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db


@pytest.fixture
def gripes(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _gripe(store: Store, text: str = "t", *, tags: tuple[str, ...] = ()) -> int:
    """The SECURITY DEFINER gripe path (ref + body + STATUS:open in one
    transaction) — insert_ref + separate tag writes trip the
    gripe_status_check trigger."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", (text,)).fetchone()
        assert row is not None
        conn.commit()
        ref_id = int(row[0])
    for t in tags:
        store.add_tag(ref_id, Tag.open(t), set_by="agent")
    return ref_id


def _set_status(store: Store, ref_id: int, status: str) -> None:
    """Flip the ref's live STATUS to ``status`` (replace_prefix keeps the
    deferred gripe_status_check invariant: exactly one STATUS)."""
    store.add_tag(
        ref_id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )


def _backdate_status(store: Store, ref_id: int, when: datetime) -> None:
    """Move the ref's STATUS tag row's set-time — the clock the closed
    window reads."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = %s FROM tags t "
            "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS'",
            (when, ref_id),
        )
        conn.commit()


def _day(days_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gr_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("gr")]


class TestStoreClosedWindow:
    """Slice 1: ``list_refs``/``count_refs`` bound the newest live
    in-set STATUS set-time, [after, before)."""

    def test_store_closed_window(self, store: Store) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2, g3 = _gripe(store), _gripe(store), _gripe(store)
        for g in (g1, g2, g3):
            _set_status(store, g, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        refs = store.list_refs(kind="gripe", closed_after=t2, closed_before=t3)
        assert [r.id for r in refs] == [g2]
        assert store.count_refs(kind="gripe", closed_after=t2, closed_before=t3) == 1

        # A reopened-then-reclosed gripe counts at its newest close —
        # the replace_prefix write deleted the old row, so the live
        # row's set-time is the reclose.
        _set_status(store, g1, "triaged")  # reopen
        _set_status(store, g1, "done")  # reclose now
        refs = store.list_refs(kind="gripe", closed_after=t2, closed_before=t3)
        assert [r.id for r in refs] == [g2]  # g1's close is "now", not t1
        refs = store.list_refs(
            kind="gripe",
            closed_after=t3,
            order_by="closed_desc",
        )
        # g1 reclosed at now, g3 at t3 — newest-closed first.
        assert [r.id for r in refs] == [g1, g3]

    def test_store_closed_bounds_semantics(self, store: Store) -> None:
        """``closed_after`` is inclusive, ``closed_before`` exclusive;
        a lone ``closed_values`` reads "currently closed"; the closed
        set defaults to done/wontfix and is overridable."""
        t1, t2 = _day(4), _day(2)
        done, wont, live = _gripe(store), _gripe(store), _gripe(store)
        _set_status(store, done, "done")
        _set_status(store, wont, "wontfix")
        _backdate_status(store, done, t1)
        _backdate_status(store, wont, t2)

        # Inclusive lower bound: t1 itself is inside [t1, t2).
        refs = store.list_refs(
            kind="gripe",
            closed_after=t1,
            closed_before=t2,
            order_by="closed_asc",
        )
        assert [r.id for r in refs] == [done]
        # Exclusive upper bound: t2 itself is outside [t1, t2)…
        # but inside [t1, t2 + 1s).
        refs = store.list_refs(
            kind="gripe",
            closed_after=t1,
            closed_before=t2 + timedelta(seconds=1),
            order_by="closed_asc",
        )
        assert [r.id for r in refs] == [done, wont]
        # Lone closed_values = "currently carries that status".
        refs = store.list_refs(kind="gripe", closed_values=["done"])
        assert [r.id for r in refs] == [done]
        # closed_values overrides the default set.
        refs = store.list_refs(kind="gripe", closed_values=["wontfix"])
        assert [r.id for r in refs] == [wont]


class TestSearchClosedWindow:
    """Slice 2: ``search(kind='gripe', status='done',
    args={'closed_since': A, 'closed_until': B})`` — queryless listing,
    newest-closed first, window in the header."""

    def test_search_closed_since_lists_rows(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2 = _gripe(store), _gripe(store)
        g3 = _gripe(store)
        for g in (g1, g2, g3):
            _set_status(store, g, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        resp = gripes.search(
            status="done",
            view="ids",
            page_size=100,
            closed_since=_iso(t2),
            closed_until=_iso(t3),
        )
        assert _gr_lines(resp.body) == [f"gr{g2}"]
        assert "closed in [" in resp.body
        assert "(by closed)" in resp.body

        # Newest-closed first across the whole set.
        resp = gripes.search(
            status="done", view="ids", page_size=100, closed_since=_iso(t1)
        )
        assert _gr_lines(resp.body) == [f"gr{g3}", f"gr{g2}", f"gr{g1}"]

        # The mint-time window is untouched: ``since`` still bounds
        # created_at on view='count'…
        resp = gripes.search(view="count", since=_iso(t2), until=_iso(t3))
        assert resp.body.startswith("# 0 gripe entries")
        # …and is still refused on the listing paths.
        with pytest.raises(BadInput, match="since="):
            gripes.search(status="done", since=_iso(t2))

    def test_no_status_uses_terminal_set(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        """Without ``status=`` the terminal set applies — the live-cohort
        default must not subtract exactly the rows asked for."""
        t1, t2 = _day(4), _day(2)
        done, wont = _gripe(store), _gripe(store)
        _gripe(store)  # stays live
        _set_status(store, done, "done")
        _set_status(store, wont, "wontfix")
        _backdate_status(store, done, t1)
        _backdate_status(store, wont, t2)

        resp = gripes.search(
            view="ids",
            page_size=100,
            closed_since=_iso(t1),
            closed_until=_iso(t2 + timedelta(seconds=1)),
        )
        assert set(_gr_lines(resp.body)) == {f"gr{done}", f"gr{wont}"}

    def test_count_view_closed_window(self, gripes: GripeHandler, store: Store) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2, g3 = _gripe(store), _gripe(store), _gripe(store)
        for g in (g1, g2, g3):
            _set_status(store, g, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        resp = gripes.search(
            view="count",
            status="done",
            closed_since=_iso(t2),
            closed_until=_iso(t3),
        )
        assert resp.body.startswith("# 1 gripe entry")
        assert "closed [" in resp.body
        # Same window, no status — the terminal set counts the same row.
        resp = gripes.search(view="count", closed_since=_iso(t2), closed_until=_iso(t3))
        assert resp.body.startswith("# 1 gripe entry")

    def test_closed_window_refused_on_ranked_and_link(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        gid = _gripe(store, "ranked probe gripe\nbody text")
        with pytest.raises(BadInput, match="closed_since"):
            gripes.search(q="ranked probe", closed_since=_iso(_day(1)))
        with pytest.raises(BadInput, match="closed_since"):
            gripes.search(link=f"gripe:{gid}", closed_since=_iso(_day(1)))


class TestStatusResolvedAlias:
    """Slice 3a: ``status='resolved'`` — the closing-comment word —
    reads as the canonical ``done``."""

    def test_status_resolved_alias(self, gripes: GripeHandler, store: Store) -> None:
        done = _gripe(store)
        live = _gripe(store)
        _set_status(store, done, "done")
        resp = gripes.search(status="resolved", view="ids", page_size=100)
        assert _gr_lines(resp.body) == [f"gr{done}"]
        assert f"gr{live}" not in resp.body

    def test_resolved_alias_negation(self, gripes: GripeHandler, store: Store) -> None:
        done = _gripe(store)
        live = _gripe(store)
        _set_status(store, done, "done")
        resp = gripes.search(status="!resolved", view="ids", page_size=100)
        assert _gr_lines(resp.body) == [f"gr{live}"]


class TestDoneListView:
    """Slice 3b: ``get(kind='gripe', id='/done')`` — STATUS:done,
    closed-newest-first, paged."""

    def test_done_list_view_order(self, gripes: GripeHandler, store: Store) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2 = _gripe(store), _gripe(store)
        g3 = _gripe(store)
        _gripe(store)  # live — off the done list
        _set_status(store, g1, "done")
        _set_status(store, g2, "done")
        _set_status(store, g3, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        body = gripes.get(id="/done").body
        pos = [body.index(f"gr{g}") for g in (g3, g2, g1)]
        assert pos == sorted(pos)
        assert "(by closed)" in body

    def test_done_list_view_window(self, gripes: GripeHandler, store: Store) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2, g3 = _gripe(store), _gripe(store), _gripe(store)
        for g in (g1, g2, g3):
            _set_status(store, g, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        body = gripes.get(
            id="/done",
            closed_since=_iso(t2),
            closed_until=_iso(t3),
        ).body
        assert f"gr{g2}" in body
        assert f"gr{g1}" not in body and f"gr{g3}" not in body
        assert "closed in [" in body

    def test_done_list_view_paged(self, gripes: GripeHandler, store: Store) -> None:
        ids = [_gripe(store) for _ in range(5)]
        for i, g in enumerate(ids):
            _set_status(store, g, "done")
            _backdate_status(store, g, _day(5 - i))
        page1 = gripes.get(id="/done", page_size=2).body
        assert "page 1 of 3" in page1
        assert "id='/done'" in page1  # the next-page call names the view
        page2 = gripes.get(id="/done", page_size=2, page=2).body
        assert "page 2 of 3" in page2

    def test_done_list_view_fallback_keeps_window_and_order(
        self, gripes: GripeHandler, store: Store
    ) -> None:
        """The grab-more fallback under a paged closure list replays the
        window and the newest-closed order — a bare
        ``search(status='done')`` would silently revert to created-time
        recency and drop the bounds (gr592737 review)."""
        ids = [_gripe(store) for _ in range(5)]
        for i, g in enumerate(ids):
            _set_status(store, g, "done")
            _backdate_status(store, g, _day(5 - i))
        since, until = _iso(_day(6)), _iso(_day(0.5))

        page1 = gripes.get(
            id="/done",
            page_size=2,
            closed_since=since,
            closed_until=until,
        ).body
        assert "next page:" in page1
        fb = page1.split("fallback:", 1)[1].strip().splitlines()[0]
        assert fb == (
            "get(kind='gripe', id='/done', args={'page_size': 100, "
            f"'closed_since': {since!r}, 'closed_until': {until!r}}})"
        )
        # The emitted call is replayable: same window, same order.
        replayed = gripes.get(
            id="/done", page_size=100, closed_since=since, closed_until=until
        ).body
        assert "(by closed)" in replayed

        # Unbounded /done: still the view call — the closure clock has
        # no bounds to drop, but the ordering survives only on this path.
        page1 = gripes.get(id="/done", page_size=2).body
        fb = page1.split("fallback:", 1)[1].strip().splitlines()[0]
        assert fb == "get(kind='gripe', id='/done', args={'page_size': 100})"

        # /wontfix with a window keeps the window too.
        for _ in range(2):
            w = _gripe(store)
            _set_status(store, w, "wontfix")
        page1 = gripes.get(id="/wontfix", page_size=1, closed_since=since).body
        assert "next page:" in page1
        fb = page1.split("fallback:", 1)[1].strip().splitlines()[0]
        assert "id='/wontfix'" in fb and f"'closed_since': {since!r}" in fb


class TestClosedWindowDispatch:
    """The args= extras channel reaches the handler through dispatch
    (``__extras__`` flattens to kwargs against the search signature)."""

    def test_search_closed_window_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        t1, t2, t3 = _day(5), _day(3), _day(1)
        g1, g2 = _gripe(store), _gripe(store)
        g3 = _gripe(store)
        for g in (g1, g2, g3):
            _set_status(store, g, "done")
        _backdate_status(store, g1, t1)
        _backdate_status(store, g2, t2)
        _backdate_status(store, g3, t3)

        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "status": "done",
                "page_size": 100,
                "__extras__": {
                    "closed_since": _iso(t2),
                    "closed_until": _iso(t3),
                },
            },
        )
        assert f"gr{g2}" in body
        assert f"gr{g1}" not in body and f"gr{g3}" not in body
        assert "[error" not in body

    def test_count_closed_window_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        g = _gripe(store)
        _set_status(store, g, "done")
        _backdate_status(store, g, _day(1))
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "gripe",
                "view": "count",
                "status": "done",
                "__extras__": {"closed_since": _iso(_day(2))},
            },
        )
        assert body.startswith("# 1 gripe entry")

    def test_done_view_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        g = _gripe(store)
        _set_status(store, g, "done")
        body = runtime_with_store.dispatch("get", {"kind": "gripe", "id": "/done"})
        assert f"gr{g}" in body
        assert "[error" not in body

    def test_closed_args_refused_on_single_get(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        g = _gripe(store)
        _set_status(store, g, "done")
        body = runtime_with_store.dispatch(
            "get",
            {
                "kind": "gripe",
                "id": g,
                "__extras__": {"closed_since": _iso(_day(1))},
            },
        )
        assert "closed_since" in body
