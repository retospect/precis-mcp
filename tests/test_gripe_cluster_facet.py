"""A gripe list that overflows its page names its ``cluster:`` groups.

The ``clusters:`` line under the header counts the *whole* filtered set
(not just the shown page) and ends in a pasteable narrowing call, so a
100-gripe queue is one ``tags=[…, 'cluster:<name>']`` away from a
workable slice.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers import _numeric_ref
from precis.handlers.gripe import GripeHandler
from precis.store import Tag
from tests._refs import insert_gripe
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _gripe(handler: GripeHandler, title: str, cluster: str | None = None) -> int:
    ref = insert_gripe(handler.store, title)
    if cluster:
        handler.store.add_tag(ref.id, Tag.open(f"cluster:{cluster}"), set_by="system")
    return int(ref.id)


def test_overflowing_list_names_clusters(handler: GripeHandler) -> None:
    for i in range(3):
        _gripe(handler, f"alpha {i}", "alpha")
    _gripe(handler, "beta 0", "beta")
    _gripe(handler, "loose 0")
    body = handler.search(tags=["STATUS:open"], page_size=2).body
    assert "clusters: cluster:alpha 3 · cluster:beta 1 · (no cluster) 1" in body
    assert "narrow: tags=['STATUS:open', 'cluster:alpha']" in body


def test_line_stops_at_coverage_and_folds_the_tail(handler: GripeHandler) -> None:
    for i in range(10):
        _gripe(handler, f"alpha {i}", "alpha")
    _gripe(handler, "beta 0", "beta")
    _gripe(handler, "gamma 0", "gamma")
    body = handler.search(tags=["STATUS:open"], page_size=2).body
    # 10/12 < 90%, alpha+beta = 11/12 ≥ 90% → gamma folds into the tail.
    assert "clusters: cluster:alpha 10 · cluster:beta 1 · … +1 groups (1)" in body
    assert "cluster:gamma 1" not in body


def test_line_caps_group_count(
    handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_numeric_ref, "FACET_LINE_MAX_GROUPS", 2)
    for name in ("a", "b", "c", "d"):
        _gripe(handler, f"{name} 0", name)
    body = handler.search(tags=["STATUS:open"], page_size=2).body
    assert "… +2 groups (2)" in body
    assert "cluster:c 1" not in body


def test_page_that_fits_has_no_cluster_line(handler: GripeHandler) -> None:
    _gripe(handler, "alpha 0", "alpha")
    body = handler.search(tags=["STATUS:open"], page_size=10).body
    assert "clusters:" not in body


def test_overflow_without_any_cluster_has_no_line(handler: GripeHandler) -> None:
    for i in range(3):
        _gripe(handler, f"loose {i}")
    body = handler.search(tags=["STATUS:open"], page_size=2).body
    assert "more past this page" in body
    assert "clusters:" not in body


def _put(handler: GripeHandler, text: str, cluster: str) -> None:
    """A gripe with a body chunk (``q=`` ranks on chunks), tagged ``cluster``."""
    rid = id_of(handler.put(text=text).body)
    handler.store.add_tag(rid, Tag.open(f"cluster:{cluster}"), set_by="system")


def test_ranked_search_overflow_names_clusters_of_the_ranked_set(
    handler: GripeHandler,
) -> None:
    for i in range(3):
        _put(handler, f"needle alpha probe {i}", "alpha")
    _put(handler, "needle beta probe", "beta")
    for i in range(4):
        _put(handler, f"unrelated gamma thing {i}", "gamma")
    body = handler.search(q="needle", tags=["STATUS:open"], page_size=2).body
    assert "clusters: cluster:alpha 3 · cluster:beta 1" in body
    assert "cluster:gamma" not in body
    assert "narrow: tags=['STATUS:open', 'cluster:alpha']" in body


def test_ranked_search_that_fits_has_no_cluster_line(handler: GripeHandler) -> None:
    _put(handler, "needle alpha probe", "alpha")
    body = handler.search(q="needle", tags=["STATUS:open"], page_size=10).body
    assert "clusters:" not in body
