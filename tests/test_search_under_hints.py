"""The search hints that ride on hub / summary-node eyes and write responses."""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, _created_id
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.eye_render import _NEIGHBOR_GROUP_CAP, render_eye
from precis.utils.memory_hubs import link_hints
from tests.test_search_under import _h, _Tree


@pytest.fixture
def tree(runtime_with_store: PrecisRuntime, store: Store) -> _Tree:
    return _Tree(runtime_with_store, store)


def test_hub_listing_ends_with_a_search_within_call_that_works(tree: _Tree) -> None:
    out = render_eye(tree.store, _h(tree.hub), "fisheye+1hop")
    assert out.splitlines()[-1] == (
        "search within: search(kind='memory', q='<terms>', "
        f"args={{'under': '{_h(tree.hub)}'}})"
    )
    hit = tree.rt.dispatch(
        "search",
        {"kind": "memory", "q": "zebra", "__extras__": {"under": _h(tree.hub)}},
    )
    assert "Pump detail" in hit and "Stray gotcha" not in hit


def test_overflowing_children_of_a_summary_node_offer_an_under_search(
    tree: _Tree,
) -> None:
    handler = MemoryHandler(hub=tree.rt.hub)
    for i in range(_NEIGHBOR_GROUP_CAP + 2):
        c = _created_id(handler.put(text="kid", title=f"Kid {i}", tags=[SPACE_TAG]))
        tree.store.add_link(src_ref_id=c, dst_ref_id=tree.summary, relation="part-of")
    out = render_eye(tree.store, _h(tree.summary), "fisheye+1hop")
    more = next(ln for ln in out.splitlines() if "more" in ln and "…" in ln)
    assert f"args={{'under': '{_h(tree.summary)}'}}" in more


def test_thread_hint_offers_gotchas_deep_in_the_hubs_subtree(tree: _Tree) -> None:
    handler = MemoryHandler(hub=tree.rt.hub)
    calls = [c for c, _d in link_hints(handler, tree.summary)]
    # the detail gotcha (a grandchild of the hub) and the direct one are offered
    assert any(f"id='{_h(tree.detail)}'" in c for c in calls)
    assert any(f"id='{_h(tree.direct)}'" in c for c in calls)
    assert not any(f"id='{_h(tree.stray)}'" in c for c in calls)
