"""``search(kind=K, view='count')`` on kinds with no native count view
(gr593053).

The slug-addressed block kinds (pres, structure, draft, conv, web, and
plugin kinds like pathway) search blocks, not refs, so their handlers
never declared ``view=`` and the weekly report's per-kind totals died on
"does not accept ['view']". The dispatch-level fallback counts live
refs through ``store.count_refs_facets`` — same shape as the native
count view, with ``tags=``/``since=``/``until=`` filters and ``group_by``
facets — and refuses ``q=`` block-match counts loudly rather than
counting the wrong set.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from precis.runtime import PrecisRuntime
from precis.store import Store, Tag


def _pres(store: Store, n: int = 1, *, retired: bool = False) -> list[int]:
    out = []
    for _ in range(n):
        ref = store.insert_ref(
            kind="pres",
            slug=f"deck-{uuid4().hex[:8]}",
            title="t",
            meta={},
        )
        if retired:
            store.retire_ref(ref.id)
        out.append(ref.id)
    return out


def _backdate(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


def test_generic_count_totals_live_refs(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _pres(store, 2)
    _pres(store, 1, retired=True)
    body = runtime_with_store.dispatch("search", {"kind": "pres", "view": "count"})
    assert body.startswith("# 2 pres entries"), body
    assert "— live refs" in body


def test_generic_count_covers_other_block_kinds(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    store.insert_ref(kind="structure", slug=f"st-{uuid4().hex[:8]}", title="s", meta={})
    store.insert_ref(kind="draft", slug=f"dr-{uuid4().hex[:8]}", title="d", meta={})
    store.insert_ref(kind="conv", slug=f"c-{uuid4().hex[:8]}", title="c", meta={})
    for kind, expected in (
        ("structure", "# 1 structure entry"),
        ("draft", "# 1 draft entry"),
        ("conv", "# 1 conv entry"),
    ):
        body = runtime_with_store.dispatch("search", {"kind": kind, "view": "count"})
        assert body.startswith(expected), (kind, body)


def test_generic_count_filters_by_tags(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    plain, tagged = _pres(store, 2)
    store.add_tag(tagged, Tag.open("subtype:slides"), set_by="agent")
    body = runtime_with_store.dispatch(
        "search",
        {"kind": "pres", "view": "count", "tags": ["subtype:slides"]},
    )
    assert body.startswith("# 1 pres entry"), body
    assert "subtype:slides" in body


def test_generic_count_group_by_facet(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    plain, tagged = _pres(store, 2)
    store.add_tag(tagged, Tag.open("subtype:slides"), set_by="agent")
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "pres",
            "view": "count",
            "__extras__": {"group_by": "tag:subtype"},
        },
    )
    assert body.startswith("# 2 pres entries"), body
    assert "tag:subtype" in body
    assert "subtype:slides: 1" in body
    assert "(untagged): 1" in body


def test_generic_count_since_until_bounds_created_at(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    old, recent = _pres(store, 2)
    _backdate(store, old, datetime.now(UTC) - timedelta(days=30))
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "pres",
            "view": "count",
            "since": (datetime.now(UTC) - timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
    )
    assert body.startswith("# 1 pres entry"), body


def test_generic_count_refuses_block_match_q(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _pres(store, 1)
    body = runtime_with_store.dispatch(
        "search", {"kind": "pres", "view": "count", "q": "slides"}
    )
    assert "[error:BadInput]" in body
    assert "q= is not supported with view='count'" in body
    assert "search(kind='pres', q='...')" in body


def test_generic_count_refuses_unknown_extras(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _pres(store, 1)
    body = runtime_with_store.dispatch(
        "search",
        {"kind": "pres", "view": "count", "__extras__": {"mode": "x"}},
    )
    assert "[error:BadInput]" in body
    assert "'mode'" in body


def test_generic_count_honours_folder_scope(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``folder=`` folds into the top-level ``include_ref_ids`` channel
    in the count interception — the generic path must count the scope,
    not the whole kind."""
    folder = store.insert_ref(kind="folder", slug=None, title="F", meta={}).id
    inside, outside = _pres(store, 2)
    store.set_parent(inside, folder)
    body = runtime_with_store.dispatch(
        "search", {"kind": "pres", "view": "count", "folder": folder}
    )
    assert body.startswith("# 1 pres entry"), body


def test_generic_count_honours_resolved_id_channels(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Top-level ``include_ref_ids``/``exclude_ref_ids`` — the channel
    ``under=``/``cited=``/``uncited=``/``hubbed=`` resolve into — must
    bind the generic count."""
    a, b, _c = _pres(store, 3)
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "pres",
            "view": "count",
            "include_ref_ids": [a, b],
            "exclude_ref_ids": [b],
        },
    )
    assert body.startswith("# 1 pres entry"), body


def test_generic_count_empty_include_is_zero(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """An empty inclusion list is a real scope — zero matches, not an
    unscoped count of the whole kind."""
    _pres(store, 3)
    body = runtime_with_store.dispatch(
        "search",
        {"kind": "pres", "view": "count", "include_ref_ids": []},
    )
    assert body.startswith("# 0 pres entries"), body


def test_generic_count_intersects_top_and_extras_includes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A caller's own ``args={'include_ref_ids': …}`` narrows the
    dispatch-resolved scope further — both bind."""
    a, b, c = _pres(store, 3)
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "pres",
            "view": "count",
            "include_ref_ids": [a, b],
            "__extras__": {"include_ref_ids": [b, c]},
        },
    )
    assert body.startswith("# 1 pres entry"), body


def test_generic_count_extras_exclude_ref_ids(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a, b, _c = _pres(store, 3)
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "pres",
            "view": "count",
            "__extras__": {"exclude_ref_ids": [a, b]},
        },
    )
    assert body.startswith("# 1 pres entry"), body


def test_generic_count_tolerates_injected_page_one(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``page=1`` is the MCP ``search`` verb's unconditional injected
    default (tools/core.py's payload) — treating it as a paging ask
    refused every ``view='count'`` on a block kind over the wire
    (gr663484). It must read as absent; only ``page`` past the first
    is a real refusal."""
    _pres(store, 2)
    body = runtime_with_store.dispatch(
        "search", {"kind": "pres", "view": "count", "page": 1}
    )
    assert body.startswith("# 2 pres entries"), body


def test_generic_count_still_refuses_past_first_page(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``page>1`` names a page the ref count can't serve — still a
    loud refusal, same as before."""
    _pres(store, 3)
    body = runtime_with_store.dispatch(
        "search", {"kind": "pres", "view": "count", "page": 2}
    )
    assert "[error:BadInput]" in body
    assert "page= is not supported" in body


def test_generic_count_head_parses_like_native(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The machine contract — the head line parses the same way a
    native count head does, so count consumers read one shape."""
    _pres(store, 3)
    body = runtime_with_store.dispatch("search", {"kind": "pres", "view": "count"})
    m = re.match(r"^# (\d+) pres entries", body)
    assert m and int(m.group(1)) == 3
