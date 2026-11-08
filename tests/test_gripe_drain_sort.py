"""gr595573: ``/gripes`` and the MCP gripe browse share the drain order.

The ruling: the queue drains — default order on ``/gripes`` AND
``get(kind='gripe', id='/open')`` / ``search(kind='gripe')`` is explicit
PRIO first where set (lower = hotter), then size ``s`` < ``m`` < ``l`` <
unsized, then oldest first within a size. ``sort=`` takes the same
vocabulary on both surfaces (``drain`` | ``oldest`` | ``newest`` |
``size`` | ``source`` | ``status``); the ordering SQL comes from one
helper (``precis.utils.gripe_order``) so the surfaces can't drift.
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
    """The pinned default: explicit PRIO first where set, then size
    s<m<l<unsized, then oldest first — identical on ``_rows`` (the
    /gripes query), ``get(id='/open')`` and ``search(kind='gripe')``."""
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


def test_open_listing_takes_the_shared_sort_vocabulary(store: Store, hub: Hub) -> None:
    """td596398 review: ``get(id='/open')`` hardcoded drain. Now
    ``args={'sort': …}`` re-groups it with the same vocabulary
    ``search(sort=)`` and ``/gripes?sort=`` take — plus the legacy
    ``prio``/``recency`` spellings."""
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
    for sort in ("drain", "oldest", "size", "status"):
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
    """The pre-drain spellings stay accepted — ``prio`` keeps its
    coalesce-to-5 semantics, ``recency`` the newest-touched order."""
    handler = GripeHandler(hub=hub)
    _seed(store, "legacy-cold prio9", prio=9)
    _seed(store, "legacy-unset")
    assert "(by prio)" in handler.search(sort="prio").body
    assert "(by recency)" in handler.search(sort="recency").body


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
