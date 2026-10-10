"""Cut/overflow markers that name the call returning the rest, and the
q-less ``search(kind='memory', view='index')`` listing."""

from __future__ import annotations

import re

import pytest

from precis.cli.memory import SPACE_TAG, _created_id
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry
from precis.utils.eye_render import (
    _NEIGHBOR_GROUP_CAP,
    _VERBATIM_CAP,
    render_eye,
)


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def _h(i: int) -> str:
    h = handle_registry.try_format("memory", i)
    assert h is not None
    return h


def _note(handler: MemoryHandler, title: str, body: str = "b", *tags: str) -> int:
    return _created_id(handler.put(text=body, title=title, tags=[SPACE_TAG, *tags]))


def test_overflow_line_carries_a_call_that_lists_the_whole_group(
    runtime: PrecisRuntime, store: Store
) -> None:
    handler = MemoryHandler(hub=runtime.hub)
    thread = _note(handler, "Thread", "t", "section:threads")
    n = _NEIGHBOR_GROUP_CAP + 3
    for i in range(n):
        g = _note(handler, f"Gotcha {i}", "g", "section:gotchas")
        store.add_link(src_ref_id=g, dst_ref_id=thread, relation="qualifies")

    out = render_eye(store, _h(thread), "fisheye+1hop")

    more = next(ln for ln in out.splitlines() if "… +3 more" in ln)
    assert f"get(kind='memory', id='{_h(thread)}'" in more
    assert "q='qualified-by'" in more
    assert (
        "search(kind='memory', tags=['SPACE:repo-dev', 'section:gotchas'], q='<terms>')"
        in more
    )

    # the advertised call really resolves, through the dispatcher
    full = runtime.dispatch(
        "get",
        {
            "kind": "memory",
            "id": _h(thread),
            "extent": "fisheye+1hop",
            "q": "qualified-by",
        },
    )
    assert full.count("qualified-by: me") == n
    assert "more" not in full.split("— linked")[-1]
    # an unknown group names the known ones
    bad = runtime.dispatch(
        "get",
        {"kind": "memory", "id": _h(thread), "extent": "fisheye+1hop", "q": "nope"},
    )
    assert "qualified-by" in bad


def test_cut_body_names_the_full_read_and_the_split(
    runtime: PrecisRuntime, store: Store
) -> None:
    handler = MemoryHandler(hub=runtime.hub)
    big = _note(handler, "Big", "x " * _VERBATIM_CAP)
    small = _note(handler, "Small", "short")

    out = render_eye(store, _h(big), "fisheye")

    assert re.search(
        rf"… cut: {_VERBATIM_CAP} of \d+ chars — full body: "
        rf"get\(kind='memory', id='{_h(big)}'\) · split it: a summary node "
        r"\+ part-of children",
        out,
    )
    assert "… cut" not in render_eye(store, _h(small), "fisheye")
    # the named call returns the whole body
    full = runtime.dispatch("get", {"kind": "memory", "id": _h(big)})
    assert full.count("x ") > _VERBATIM_CAP // 2


def test_index_search_without_q_lists_by_recency(
    runtime: PrecisRuntime, store: Store
) -> None:
    handler = MemoryHandler(hub=runtime.hub)
    old = _note(handler, "Old one", "o", "section:gotchas")
    new = _note(handler, "New one", "n", "section:gotchas")
    other = _note(handler, "Other type", "x", "section:threads")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '9 days' WHERE ref_id = %s",
            (old,),
        )

    out = runtime.dispatch(
        "search",
        {
            "kind": "memory",
            "view": "index",
            "tags": [SPACE_TAG, "section:gotchas"],
        },
    )

    lines = out.splitlines()
    assert [ln.split(" (")[0] for ln in lines] == ["- New one", "- Old one"]
    assert _h(other) not in out
    assert _h(new) in lines[0]
