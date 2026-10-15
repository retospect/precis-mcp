"""Real-PG tests for the graph focus trail (web-graph-navigation slice 6).

The focus page keeps a cookie-held breadcrumb of recent focus pages
(``graph_trail``, no server storage). Pinned here (AC 6): after visiting
three focus pages the trail lists them in order and each crumb
re-focuses; plus retreat = the crumb before the current one, a crumb
click truncating the trail back to it, the length cap, an untrusted
cookie's junk dropped and the path page showing the trail read-only.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.graph import TRAIL_COOKIE, TRAIL_MAX


@pytest.fixture
def graph_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _memories(store: Store, n: int) -> list[int]:
    return [
        store.insert_ref(kind="memory", slug=None, title=f"m{i}").id for i in range(n)
    ]


def _h(ref_id: int) -> str:
    return handle_registry.format_handle("memory", ref_id)


def _trail_nav(html_text: str) -> str:
    m = re.search(r'<nav[^>]*aria-label="focus trail".*?</nav>', html_text, re.DOTALL)
    assert m, "no focus trail on the page"
    return m.group(0)


def _crumbs(nav: str) -> list[str]:
    """Trail handles in render order, the current (unlinked) one included."""
    pairs = re.findall(r'data-crumb="([^"]+)"|aria-current="page">([^<]+)<', nav)
    return [a or b for a, b in pairs]


def _retreat(nav: str) -> str | None:
    m = re.search(r'<a href="([^"]+)"[^>]*aria-label="retreat"', nav)
    return m.group(1) if m else None


def test_three_visits_list_in_order_and_crumbs_refocus(
    graph_client: TestClient, store: Store
) -> None:
    a, b, c = _memories(store, 3)
    for i in (a, b):
        assert graph_client.get(f"/graph/memory/{i}").status_code == 200
    r = graph_client.get(f"/graph/memory/{c}")
    nav = _trail_nav(r.text)
    assert _crumbs(nav) == [_h(a), _h(b), _h(c)]
    # earlier crumbs are links to their focus pages; the current one is not
    assert f'href="/graph/memory/{a}"' in nav
    assert f'href="/graph/memory/{b}"' in nav
    assert f'href="/graph/memory/{c}"' not in nav
    assert _retreat(nav) == f"/graph/memory/{b}"

    # following a crumb re-focuses and truncates the trail back to it
    r = graph_client.get(f"/graph/memory/{a}")
    nav = _trail_nav(r.text)
    assert _crumbs(nav) == [_h(a)]
    assert _retreat(nav) is None


def test_retreat_walks_back_one_step(graph_client: TestClient, store: Store) -> None:
    a, b, c = _memories(store, 3)
    for i in (a, b, c):
        graph_client.get(f"/graph/memory/{i}")
    nav = _trail_nav(graph_client.get(f"/graph/memory/{b}").text)
    assert _crumbs(nav) == [_h(a), _h(b)]
    assert _retreat(nav) == f"/graph/memory/{a}"


def test_trail_is_capped(graph_client: TestClient, store: Store) -> None:
    ids = _memories(store, TRAIL_MAX + 3)
    for i in ids:
        r = graph_client.get(f"/graph/memory/{i}")
    assert _crumbs(_trail_nav(r.text)) == [_h(i) for i in ids[-TRAIL_MAX:]]


def test_untrusted_cookie_junk_and_dead_refs_drop(
    graph_client: TestClient, store: Store
) -> None:
    a, b, gone = _memories(store, 3)
    store.retire_ref(gone)
    graph_client.cookies.set(
        TRAIL_COOKIE,
        f"memory:{a}|<script>|paper:{a}|memory:{gone}|memory:abc",
        path="/graph",
    )
    nav = _trail_nav(graph_client.get(f"/graph/memory/{b}").text)
    assert _crumbs(nav) == [_h(a), _h(b)]
    assert "<script>" not in nav


def test_path_page_shows_trail_with_retreat_to_last_focus(
    graph_client: TestClient, store: Store
) -> None:
    a, b = _memories(store, 2)
    graph_client.get(f"/graph/memory/{a}")
    graph_client.get(f"/graph/memory/{b}")
    nav = _trail_nav(graph_client.get("/graph/path").text)
    assert _crumbs(nav) == [_h(a), _h(b)]
    assert _retreat(nav) == f"/graph/memory/{b}"
