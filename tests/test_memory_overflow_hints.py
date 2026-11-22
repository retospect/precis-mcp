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
    # expanded group: a count header, then one indented row per member
    assert f"— linked (1 hop) · qualified-by ({n}) —" in full
    assert len(re.findall(r"^    me\d+ — Gotcha ", full, flags=re.M)) == n
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
        rf"get\(kind='memory', id='{_h(big)}'\) · split it: home each claim in "
        r"the memory that owns it "
        r"\(get\(kind='skill', id='how-to-split-memory'\)\)$",
        out,
        flags=re.M,
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


def test_body_read_over_cap_pages_to_end(
    runtime: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr685357: an over-cap ``~body`` read pages by character offset —
    the cut footer names the next offset, following it reassembles the
    whole body, and an offset at the end refuses naming the size."""
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "1200")
    handler = MemoryHandler(hub=runtime.hub)
    body = "café λ\n" * 750
    big = _note(handler, "Big body", body)
    ref = store.get_ref(kind="memory", id=big)
    assert ref is not None
    expected = handler._display_body(ref, handler._body_text(ref))

    for view in (None, "full"):
        args = {"kind": "memory", "id": _h(big)}
        if view is not None:
            args["view"] = view
        whole = runtime.dispatch("get", args)
        assert f"next: get(kind='memory', id='{_h(big)}~body')" in whole
        assert "q='<more specific terms>'" not in whole

    out = runtime.dispatch("get", {"kind": "memory", "id": f"{_h(big)}~body"})
    assert "args={'offset':" in out
    alias = runtime.dispatch("get", {"kind": "memory", "id": _h(big), "view": "body"})
    assert re.search(r"rest: get\(kind='memory'.*args=\{'offset': \d+\}\)", alias)

    parts: list[str] = []
    offset = 0
    for _ in range(20):
        head = re.search(r"# memory \d+~body \(chars (\d+)\.\.(\d+) of (\d+)\)", out)
        assert head is not None, out
        start, end, total = (int(g) for g in head.groups())
        assert start == offset
        assert end > start
        assert total == len(expected)
        assert len(out.encode("utf-8")) <= 1200
        page = out.split("\n\n", 1)[1]
        if "\n\n… showing" in page:
            page = page.rsplit("\n\n… showing", 1)[0]
        # the record frame's close tag trails the final page (gr739333)
        page = re.sub(r"</record [^\n]*>\s*$", "", page)
        parts.append(page)
        if "rest: " not in out:
            break
        nxt = re.search(
            r"rest: get\(kind='memory', id='[^']+', args=\{'offset': (\d+)\}\)", out
        )
        assert nxt is not None, out
        assert int(nxt[1]) == end
        offset = end
        out = runtime.dispatch(
            "get", {"kind": "memory", "id": f"{_h(big)}~body", "offset": offset}
        )
    else:
        pytest.fail("body paging did not finish")
    assert "".join(parts) == expected

    past = runtime.dispatch(
        "get", {"kind": "memory", "id": f"{_h(big)}~body", "offset": len(expected)}
    )
    assert f"{len(expected)} characters" in past
