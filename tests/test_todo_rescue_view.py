"""The rescue queue (dark-factory td574946 S1): ``view='rescue'`` lists
every live node in rescue grouped by class, the work view carries a
``rescue (N)`` group, and the asks view files a rescued node as a
showstopper."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import _TREE_SEARCH_VIEWS, TodoHandler, TodoView
from precis.store import Store
from precis.store.types import Tag
from precis.todo import asks_view
from precis.workers import factory_rescue as fr
from tests._refs import insert_gripe


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _todo(store: Store, title: str) -> int:
    return int(store.insert_ref(kind="todo", slug=None, title=title).id)


def _rescue(store: Store, node: int, cls: str = "builder") -> fr.RescueResult:
    return fr.enter_rescue(
        store,
        node,
        cls,
        fr.RescueEvidence(step="run_slice", error_line="kaboom", log_tail="kaboom"),
    )


def test_rescue_view_is_wired_in_the_vocabulary() -> None:
    assert TodoView("rescue") is TodoView.RESCUE
    assert TodoView.RESCUE in _TREE_SEARCH_VIEWS


def test_rescue_view_lists_a_rescued_node(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    n = _todo(store, "Failing build")
    res = _rescue(store, n)

    out = handler.search(view="rescue").body
    assert out.startswith("# rescue (1)")
    assert f"td{n}" in out and "builder" in out and "Failing build" in out
    assert res.gripe_id is not None and f"gr{res.gripe_id}" in out

    # a gripe in rescue renders in its own class group, no sig cell
    g = int(insert_gripe(store, "intake broke").id)
    _rescue(store, g, "intake")
    out = handler.search(view="rescue").body
    assert out.startswith("# rescue (2)")
    assert "intake" in out and f"gr{g}" in out

    # closing the node drops it from the queue
    store.add_tag(n, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True)
    out = handler.search(view="rescue").body
    assert f"td{n}" not in out


def test_rescue_view_empty(handler: TodoHandler) -> None:
    assert "no node in rescue" in handler.search(view="rescue").body


def test_work_view_has_rescue_group_only_when_nonempty(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    assert "rescue (" not in handler.search(view="work").body

    n = _todo(store, "Rescued slice")
    _rescue(store, n)
    out = handler.search(view="work").body
    assert "rescue (1)" in out
    assert f"td{n}" in out


def test_work_view_rescue_group_caps_at_twenty(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    for i in range(22):
        _rescue(store, _todo(store, f"slice {i}"))
    out = handler.search(view="work").body
    assert "rescue (22)" in out
    assert "+2 more" in out
    assert "view='rescue'" in out


def test_asks_view_lists_a_rescued_node_as_showstopper(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    n = _todo(store, "Rescued slice")
    res = _rescue(store, n)

    model = asks_view.asks_model(store)
    row = next(r for r in model.rows if r.ref_id == n)
    assert row.klass == asks_view.SHOWSTOPPER
    assert "rescue:builder" in row.reasons
    # the signature gripe's rescue-sig:<hash> tag must NOT read as rescue
    assert res.gripe_id is not None
    assert all(r.ref_id != res.gripe_id for r in model.rows)

    out = handler.search(view="asks").body
    assert "showstopper" in out
    assert f"td{n}" in out
