"""gr595573: ``/gripes`` and the MCP gripe browse share the drain order.

The ruling: the queue drains — default order on ``/gripes`` AND
``get(kind='gripe', id='/open')`` / ``search(kind='gripe')`` is the
dispatch score first (a set PRIO multiplies it), then size ``s`` <
``m`` < ``l`` < unsized, then oldest first within a size. ``sort=``
takes the same vocabulary on both surfaces (``drain`` | ``oldest`` |
``newest`` | ``prio`` | ``size`` | ``source`` | ``status``); the
ordering SQL comes from one helper (``precis.utils.gripe_order``) so
the surfaces can't drift. ``prio`` (gr604514) leads the human PRIO
ladder — unset reads as normal — then size, then oldest.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.store import Store, Tag
from precis_web.routes.gripes import _rows
from tests._refs import insert_gripe


def _seed(
    store: Store,
    title: str,
    *,
    days: int = 0,
    prio: int | None = None,
    size: str | None = None,
    status: str = "open",
    source_tags: tuple[str, ...] = (),
) -> int:
    """One gripe with controllable drain keys: ``prio`` is the column,
    ``size:`` the open tag, ``days`` the filed age, ``source_tags`` the
    ``user:``/``origin:`` marks the ``source`` sort classifies on."""
    ref = insert_gripe(store, title, status=status)
    if prio is not None:
        store.set_prio(int(ref.id), prio)
    for tag in ([f"size:{size}"] if size else []) + list(source_tags):
        store.add_tag(int(ref.id), Tag.open(tag), set_by="system")
    if days:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - make_interval(days => %s) "
                "WHERE ref_id = %s",
                (days, int(ref.id)),
            )
    return int(ref.id)


def _positions(body: str, titles: list[str]) -> list[int]:
    return [body.index(t) for t in titles]


def test_drain_order_is_the_default_on_both_surfaces(store: Store, hub: Hub) -> None:
    """The pinned default: the dispatch score first (the seeded prio=2
    multiplies its gripe hottest), then size s<m<l<unsized, then oldest
    first — identical on ``_rows`` (the /gripes query),
    ``get(id='/open')`` and ``search(kind='gripe')``."""
    handler = GripeHandler(hub=hub)
    hot = _seed(store, "drain-hot unsized prio2", days=1, prio=2)
    small_old = _seed(store, "drain-small old", days=9, size="s")
    small_new = _seed(store, "drain-small new", days=1, size="s")
    mid = _seed(store, "drain-mid", days=20, size="m")
    unsized = _seed(store, "drain-unsized oldest", days=30)
    expected = [hot, small_old, small_new, mid, unsized]
    titles = [
        "drain-hot unsized prio2",
        "drain-small old",
        "drain-small new",
        "drain-mid",
        "drain-unsized oldest",
    ]

    assert [r["ref_id"] for r in _rows(store, status_filter="all")] == expected

    open_body = handler.get(id="/open").body
    assert "(by drain)" in open_body
    assert _positions(open_body, titles) == sorted(_positions(open_body, titles))

    search_body = handler.search().body
    assert _positions(search_body, titles) == sorted(_positions(search_body, titles))


def test_factory_gripe_outranks_an_equal_severity_plain_one(store: Store) -> None:
    """Reto 2026-11-12: ``cluster:dark-factory`` / ``thread:factory``
    score x3 in the live SQL — ahead of a plain gripe of equal score
    even when the plain one is older and sized small."""
    plain = _seed(store, "factory-plain", days=20, size="s")
    cluster = _seed(
        store, "factory-cluster", days=1, source_tags=("cluster:dark-factory",)
    )
    thread = _seed(store, "factory-thread", days=1, source_tags=("thread:factory",))
    for ref_id in (plain, cluster, thread):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || '{\"dispatch_score\": 4}'::jsonb "
                "WHERE ref_id = %s",
                (ref_id,),
            )
    rows = _rows(store, status_filter="all")
    by_id = {r["ref_id"]: r for r in rows}
    assert by_id[plain]["score"] == pytest.approx(4.0)
    assert by_id[cluster]["score"] == pytest.approx(12.0)
    assert by_id[thread]["score"] == pytest.approx(12.0)
    order = [r["ref_id"] for r in rows]
    assert order.index(plain) > max(order.index(cluster), order.index(thread))


def test_sort_values_reorder_the_same_queue(store: Store, hub: Hub) -> None:
    """Every ``sort=`` name works on both surfaces: ``oldest``/
    ``newest`` by filing age, ``size`` the ladder, ``source`` human <
    agent < watchdog, ``status`` the workflow rank."""
    handler = GripeHandler(hub=hub)
    old_s = _seed(
        store,
        "sort-old s watchdog",
        days=8,
        size="s",
        source_tags=("origin:ci-watch",),
    )
    mid_m = _seed(
        store, "sort-mid m human", days=4, size="m", source_tags=("user:reto",)
    )
    new_unsized = _seed(store, "sort-new unsized agent", days=1)
    titles = [
        "sort-old s watchdog",
        "sort-mid m human",
        "sort-new unsized agent",
    ]

    def web_ids(sort: str) -> list[int]:
        return [r["ref_id"] for r in _rows(store, status_filter="all", sort=sort)]

    assert web_ids("oldest") == [old_s, mid_m, new_unsized]
    assert web_ids("newest") == [new_unsized, mid_m, old_s]
    assert web_ids("size") == [old_s, mid_m, new_unsized]
    assert web_ids("source") == [mid_m, new_unsized, old_s]

    oldest = handler.search(sort="oldest").body
    assert _positions(oldest, titles) == sorted(_positions(oldest, titles))
    source = handler.search(sort="source").body
    assert (
        source.index("sort-mid m human")
        < source.index("sort-new unsized agent")
        < source.index("sort-old s watchdog")
    )

    # The row carries the values the sort ranked on — a displayed
    # size/source can't drift from the sort key.
    rows = {r["ref_id"]: r for r in _rows(store, status_filter="all", sort="drain")}
    assert rows[old_s]["size"] == "s" and rows[old_s]["source"] == "watchdog"
    assert rows[mid_m]["size"] == "m" and rows[mid_m]["source"] == "human"
    assert rows[new_unsized]["size"] is None
    assert rows[new_unsized]["source"] == "agent"


def test_prio_sort_orders_priority_then_size(store: Store, hub: Hub) -> None:
    """gr604514: ``sort=prio`` — the human PRIO ladder first (unset
    reads as the normal rung, so ``PRIO:low`` sinks under unjudged
    work), then size s<m<l<unsized, then oldest — identical on
    ``_rows`` and the MCP browse: the ``prio`` spelling resolves to
    the shared ``gripe_prio`` fragment, so the two surfaces can't
    drift."""
    handler = GripeHandler(hub=hub)
    urgent = _seed(store, "prio-urgent l", days=1, prio=1, size="l")
    high_s = _seed(store, "prio-high s new", days=1, prio=3, size="s")
    high_m = _seed(store, "prio-high m old", days=9, prio=3, size="m")
    unset_old = _seed(store, "prio-unset oldest", days=30)
    unset_new = _seed(store, "prio-unset newest", days=1)
    low = _seed(store, "prio-low s oldest", days=40, prio=8, size="s")
    expected = [urgent, high_s, high_m, unset_old, unset_new, low]
    titles = [
        "prio-urgent l",
        "prio-high s new",
        "prio-high m old",
        "prio-unset oldest",
        "prio-unset newest",
        "prio-low s oldest",
    ]

    assert [
        r["ref_id"] for r in _rows(store, status_filter="all", sort="prio")
    ] == expected

    open_body = handler.get(id="/open", sort="prio").body
    assert "(by prio)" in open_body
    assert _positions(open_body, titles) == sorted(_positions(open_body, titles))

    search_body = handler.search(sort="prio").body
    assert _positions(search_body, titles) == sorted(_positions(search_body, titles))


def test_open_listing_takes_the_shared_sort_vocabulary(store: Store, hub: Hub) -> None:
    """td596398 review: ``get(id='/open')`` hardcoded drain. Now
    ``args={'sort': …}`` re-groups it with the same vocabulary
    ``search(sort=)`` and ``/gripes?sort=`` take — plus the legacy
    ``recency`` spelling."""
    handler = GripeHandler(hub=hub)
    _seed(
        store,
        "open-sort old s watchdog",
        days=8,
        size="s",
        source_tags=("origin:ci-watch",),
    )
    _seed(
        store,
        "open-sort mid m human",
        days=4,
        size="m",
        source_tags=("user:reto",),
    )
    _seed(store, "open-sort new unsized agent", days=1)
    titles = [
        "open-sort old s watchdog",
        "open-sort mid m human",
        "open-sort new unsized agent",
    ]

    # Every seeded gripe is STATUS:open, so ``status`` ranks them all in
    # one group — oldest first inside, the same order ``oldest`` gives.
    # ``prio`` too: all unset, so the size ladder + age decide.
    for sort in ("drain", "oldest", "prio", "size", "status"):
        body = handler.get(id="/open", sort=sort).body
        assert f"(by {sort})" in body
        assert _positions(body, titles) == sorted(_positions(body, titles))

    newest = handler.get(id="/open", sort="newest").body
    assert "(by newest)" in newest
    assert _positions(newest, titles[::-1]) == sorted(_positions(newest, titles[::-1]))

    source = handler.get(id="/open", sort="source").body
    assert (
        source.index("open-sort mid m human")
        < source.index("open-sort new unsized agent")
        < source.index("open-sort old s watchdog")
    )

    assert "(by prio)" in handler.get(id="/open", sort="prio").body
    assert "(by recency)" in handler.get(id="/open", sort="recency").body


def test_open_listing_sort_survives_pagination(store: Store, hub: Hub) -> None:
    """The next-page call re-states a caller's sort= — otherwise page 2
    would silently revert to drain."""
    handler = GripeHandler(hub=hub)
    _seed(store, "page-sort newest", days=0)
    _seed(store, "page-sort middle", days=5)
    _seed(store, "page-sort oldest", days=10)

    page1 = handler.get(id="/open", page_size=2, sort="newest").body
    assert "page-sort newest" in page1 and "page-sort middle" in page1
    assert "page-sort oldest" not in page1
    assert "'sort': 'newest'" in page1
    assert "fallback: search(kind='gripe'" in page1
    assert "sort='newest'" in page1.split("fallback:", 1)[1]

    page2 = handler.get(id="/open", page=2, page_size=2, sort="newest").body
    assert "page-sort oldest" in page2
    assert "page-sort newest" not in page2


def test_open_listing_sort_refusals(store: Store, hub: Hub) -> None:
    """A sort outside the vocabulary, or on a surface with no listing to
    order, refuses — never a silent drain."""
    handler = GripeHandler(hub=hub)
    rid = _seed(store, "refused sort target")
    with pytest.raises(BadInput, match="unknown sort"):
        handler.get(id="/open", sort="bogus")
    for bad_id in ("/parked", "/queue", "/batch", "/recent", rid):
        with pytest.raises(BadInput):
            handler.get(id=bad_id, sort="drain")


def test_status_sort_ranks_the_workflow(store: Store, hub: Hub) -> None:
    """``sort='status'`` groups by the workflow rank — open before
    triaged before the terminal done — oldest first inside."""
    handler = GripeHandler(hub=hub)
    done = _seed(store, "rank-done", status="done", days=30)
    open_ = _seed(store, "rank-open", days=2)
    triaged = _seed(store, "rank-triaged", status="triaged", days=20)

    got = [r["ref_id"] for r in _rows(store, status_filter="all", sort="status")]
    assert got == [open_, triaged, done]
    body = handler.search(status="*", sort="status").body
    assert (
        body.index("rank-open") < body.index("rank-triaged") < body.index("rank-done")
    )


def test_legacy_prio_and_recency_sorts_still_work(store: Store, hub: Hub) -> None:
    """The ``prio``/``recency`` spellings stay accepted — ``prio`` is
    now a shared-vocabulary name (still unset-reads-as-5, with a size
    tie-break), ``recency`` the legacy newest-touched order."""
    handler = GripeHandler(hub=hub)
    _seed(store, "legacy-cold prio9", prio=9)
    _seed(store, "legacy-unset")
    assert "(by prio)" in handler.search(sort="prio").body
    assert "(by recency)" in handler.search(sort="recency").body


def test_gripe_prio_takes_every_value_1_to_10(store: Store, hub: Hub) -> None:
    """gr632811: prio is 1..10 on every kind, gripes included — every
    value writes on both spellings and both verbs; out-of-range still
    refuses, and the names are aliases for 1/3/5/8."""
    handler = GripeHandler(hub=hub)
    rid = _seed(store, "range target")
    for n in range(1, 11):
        assert handler.put(text=f"prio={n} filing", prio=n)
        assert handler.put(text=f"PRIO:{n} filing", tags=[f"PRIO:{n}"])
        handler.tag(id=rid, prio=n)
        ref = store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.prio == n
        handler.tag(id=rid, add=[f"PRIO:{n}"])
        ref = store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.prio == n
    for bad in (0, 11):
        with pytest.raises(BadInput, match="1..10"):
            handler.put(text=f"bad prio={bad}", prio=bad)
        with pytest.raises(BadInput, match="1..10"):
            handler.tag(id=rid, prio=bad)
    for name, n in (("urgent", 1), ("high", 3), ("normal", 5), ("low", 8)):
        handler.tag(id=rid, add=[f"PRIO:{name}"])
        ref = store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.prio == n


def test_gripe_prio_remove_still_clears(store: Store, hub: Hub) -> None:
    """A ``PRIO:*`` remove clears the column whatever value it names."""
    handler = GripeHandler(hub=hub)
    rid = _seed(store, "clear target", prio=3)
    handler.tag(id=rid, remove=["PRIO:7"])
    ref = store.get_ref(kind="gripe", id=rid)
    assert ref is not None and ref.prio is None


def test_unknown_sort_refused_on_both_surfaces(
    store: Store, hub: Hub, runtime_with_store: Any, tmp_path: Any
) -> None:
    """A ``sort=`` outside the shared vocabulary is a named refusal —
    a 400 listing the allowed set on the web, a BadInput naming the
    gripe sorts on the MCP — never a silent wrong order."""
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    client = TestClient(
        create_app(
            runtime=runtime_with_store,
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )
    for url in ("/gripes?sort=bogus", "/gripes/rows?status=open&sort=bogus"):
        resp = client.get(url)
        assert resp.status_code == 400, url
        assert "drain" in resp.text and "source" in resp.text

    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match="sort= is supported"):
        handler.search(sort="bogus")
    with pytest.raises(BadInput, match="sort= is supported"):
        handler.search(q="ranked", sort="drain")  # browse sort + q= refuses
