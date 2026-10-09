"""MemoryHandler — through both direct invocation and full dispatch."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, Internal, NotFound
from precis.handlers.memory import MemoryHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.store import Store, as_dream_actor
from tests.conftest import id_of

# ---------------------------------------------------------------------------
# Handler-level tests (skip MCP, skip dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def test_create_returns_id(handler: MemoryHandler) -> None:
    r = handler.put(text="my first memory")
    # the ack now surfaces the universal handle (``me<id>``).
    assert "created memory me" in r.body


def test_create_requires_text(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.put()


def test_create_then_get(handler: MemoryHandler) -> None:
    r = handler.put(text="findable memory body")
    # parse out the id
    new_id = id_of(r.body)

    got = handler.get(id=new_id)
    assert "findable memory body" in got.body
    assert f"memory {new_id}" in got.body


def test_get_unknown_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id=99999)


def test_get_string_id_coerced(handler: MemoryHandler) -> None:
    r = handler.put(text="numeric coercion")
    new_id = id_of(r.body)
    got = handler.get(id=str(new_id))
    assert "numeric coercion" in got.body


def test_get_bad_id(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="integer"):
        handler.get(id="not-a-number")


def test_put_on_existing_id_rejected(handler: MemoryHandler) -> None:
    """Seven-verb cutover: put is creation-only on numeric kinds.

    Mutating an existing memory's text body is no longer exposed —
    capture the new wording as a fresh memory or use
    ``delete + put`` to replace. Tag/link mutation moves to the
    dedicated verbs.
    """
    r = handler.put(text="original")
    new_id = id_of(r.body)

    with pytest.raises(BadInput, match="put on existing memory"):
        handler.put(id=new_id, text="updated")

    # The original text is untouched.
    got = handler.get(id=new_id)
    assert "original" in got.body


def test_update_tags_only(handler: MemoryHandler) -> None:
    # The closed-axis allow-list for memory is empty (per-kind axis
    # enforcement) — memory uses only open tags. Demonstrate updating
    # a memory with two open tags from the documented vocabulary.
    r = handler.put(text="memory with tags")
    new_id = id_of(r.body)

    handler.tag(id=new_id, add=["kind:decision", "confidence-strong"])

    got = handler.get(id=new_id)
    assert "kind:decision" in got.body
    assert "confidence-strong" in got.body


def test_update_open_tags_accumulate(handler: MemoryHandler) -> None:
    """Open tags accumulate (no axis-replacement contract). Two
    confidence-* tags can coexist — the agent is responsible for
    untagging the old value, exactly the pattern documented in
    ``precis-memory-help`` for the open-tag confidence axis."""
    r = handler.put(text="x", tags=["confidence-tentative"])
    new_id = id_of(r.body)

    handler.tag(id=new_id, add=["confidence-strong"])

    got = handler.get(id=new_id)
    # Both stick around — that's the open-tag contract. The
    # remove-on-update workflow uses ``untags=`` (see
    # test_untags_on_put.py).
    assert "confidence-tentative" in got.body
    assert "confidence-strong" in got.body


def test_status_axis_rejected_on_memory(handler: MemoryHandler) -> None:
    """Per-kind axis enforcement: STATUS: belongs on todo/gripe,
    not on memory. The MCP critic flagged ``STATUS:open`` on a memory
    as a smell — the tag is decorative because no STATUS-filtered
    query against ``kind='memory'`` can find it. Reject at the write
    boundary instead."""
    r = handler.put(text="m")
    new_id = id_of(r.body)
    with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
        handler.tag(id=new_id, add=["STATUS:open"])


def test_tag_no_op_rejected(handler: MemoryHandler) -> None:
    """``tag()`` with no add= and no remove= is a misuse — reject
    rather than silently no-op so a typo doesn't vanish."""
    r = handler.put(text="x")
    new_id = id_of(r.body)
    with pytest.raises(BadInput, match="requires add= or remove="):
        handler.tag(id=new_id)


def test_delete(handler: MemoryHandler) -> None:
    r = handler.put(text="goodbye")
    new_id = id_of(r.body)

    deleted = handler.delete(id=new_id)
    assert "deleted" in deleted.body

    # MCP critic MINOR-C (round 1): soft-deleted refs raise ``Gone``
    # (distinct from ``NotFound`` for never-existed ids). The row is
    # still addressable at the SQL layer by clearing ``retired_at``.
    with pytest.raises(Gone, match="soft-deleted"):
        handler.get(id=new_id)


def test_delete_missing_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.delete(id=99999)


def test_search_finds_match(handler: MemoryHandler) -> None:
    handler.put(text="nitrate reduction on copper")
    handler.put(text="something completely different")

    r = handler.search(q="nitrate copper")
    assert "nitrate" in r.body
    assert "1 memory" in r.body


def test_search_no_match(handler: MemoryHandler) -> None:
    handler.put(text="hello world")
    r = handler.search(q="frobnicate")
    assert "no memory entries match" in r.body


def test_search_requires_q(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()
    with pytest.raises(BadInput):
        handler.search(q="   ")


# ---------------------------------------------------------------------------
# Card emission (dreaming foundation): memories become embeddable
# ---------------------------------------------------------------------------


def test_create_emits_memory_body_chunk(handler: MemoryHandler) -> None:
    """A new memory's prose lives in a ``memory_body`` chunk (``ord=0``), not
    ``refs.title`` (migration 0050). That chunk is the single embed source —
    the embed + chunk_keywords workers index it automatically, so semantic
    search finds true neighbours and dreams live in a chunk, not the header.
    No ``card_combined`` card any more (it would double-embed the prose)."""
    body = "electrochemical CO2 reduction on copper"
    r = handler.put(text=body, title="CO2 reduction on Cu")
    new_id = id_of(r.body)
    with handler.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id = %s",
            (new_id,),
        ).fetchall()
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (new_id,)
        ).fetchone()
    assert rows == [(0, "memory_body", body)]
    # The header is the short title, the prose is in the chunk.
    assert title == ("CO2 reduction on Cu",)


def test_create_attributes_touch_when_agentlog_env_set(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory create attributes its body chunk to the current run when
    ``PRECIS_CURRENT_AGENTLOG`` is set — so a dream's own memories walk
    back to the tick that wrote them (Slice B)."""
    from precis import agentlog

    log_id = agentlog.open_log(handler.store, source="dream", title="t")
    monkeypatch.setenv(agentlog.ENV_VAR, str(log_id))

    handler.put(text="a dream-spawned memory")

    links = handler.store.links_for(log_id, direction="out", relation="touched")
    assert len(links) == 1


def test_create_no_touch_and_no_error_without_agentlog_env(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``PRECIS_CURRENT_AGENTLOG`` (a normal agent/console create) is a
    silent no-op — no link, no error."""
    monkeypatch.delenv("PRECIS_CURRENT_AGENTLOG", raising=False)
    r = handler.put(text="an ordinary memory")
    assert "created memory me" in r.body
    with handler.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'touched'"
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_create_derives_title_when_omitted(handler: MemoryHandler) -> None:
    """Omitting title= derives one from the body's first line (capped 80)."""
    body = "Copper facets steer CO2RED selectivity.\n\nThe (100) face favours…"
    r = handler.put(text=body)
    new_id = id_of(r.body)
    with handler.store.pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (new_id,)
        ).fetchone()
    assert title == ("Copper facets steer CO2RED selectivity.",)


def test_upsert_card_combined_is_idempotent(store: Store) -> None:
    """Re-emitting replaces (DELETE+INSERT), never duplicates: a memory
    keeps exactly one ``ord=-1`` card, with the latest text."""
    ref = store.insert_ref(kind="memory", slug=None, title="first", meta={})
    store.chunks.upsert_card_combined(ref.id, "first")
    store.chunks.upsert_card_combined(ref.id, "second")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, text FROM chunks WHERE ref_id = %s ORDER BY ord",
            (ref.id,),
        ).fetchall()
    assert rows == [(-1, "second")]


# ---------------------------------------------------------------------------
# supersede: the one guarded destructive merge (dreaming)
# ---------------------------------------------------------------------------


def _id_of(resp: Response) -> int:
    """Parse the id off a ``put`` create-ack (``...id=N``).

    Delegates to the shared :func:`tests.conftest.id_of` helper which
    handles trailing punctuation in the TOON ``Next:`` trailer
    (``delete(..., id=N)``) — historical inline ``rsplit("=", 1)[1]``
    parses broke when the trailer changed.
    """
    return id_of(resp.body)


def _new_id(resp: Response) -> int:
    """Parse the survivor id off a ``supersede`` ack (``...id=N (...)``)."""
    return id_of(resp.body)


def test_supersede_merges_and_soft_deletes(
    handler: MemoryHandler, store: Store
) -> None:
    a = _id_of(handler.put(text="nitrate reduces on copper"))
    b = _id_of(handler.put(text="copper reduces nitrate"))
    r = handler.supersede(merge_ids=[a, b], new_text="Cu reduces nitrate")
    new_id = _new_id(r)
    # survivor is a live memory carrying its text + DREAM:consolidated
    got = handler.get(id=new_id)
    assert "Cu reduces nitrate" in got.body
    assert any(str(t) == "DREAM:consolidated" for t in store.tags_for(new_id))
    # originals are soft-deleted and point back via supersedes edges
    assert store.get_ref(kind="memory", id=a) is None
    assert store.get_ref(kind="memory", id=b) is None
    sup = store.links_for(new_id, direction="out", relation="supersedes")
    assert {link.dst_ref_id for link in sup} == {a, b}
    superseded_a = store.get_ref(kind="memory", id=a, include_deleted=True)
    assert superseded_a is not None
    assert superseded_a.meta["superseded_by"] == new_id


def test_supersede_migrates_links(handler: MemoryHandler, store: Store) -> None:
    keep = _id_of(handler.put(text="keep this neighbour"))
    a = _id_of(handler.put(text="dup one"))
    b = _id_of(handler.put(text="dup two"))
    store.add_link(src_ref_id=a, dst_ref_id=keep, relation="related-to")
    new_id = _new_id(handler.supersede(merge_ids=[a, b], new_text="dup"))
    # a -> keep migrated onto survivor -> keep
    out = store.links_for(new_id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == keep for link in out)
    # the original edge off `a` is gone (only the supersedes edge remains)
    assert not [
        link
        for link in store.links_for(a, relation="related-to")
        if link.relation == "related-to"
    ]


def test_supersede_default_tags_union(handler: MemoryHandler, store: Store) -> None:
    a = _id_of(handler.put(text="alpha", tags=["topic:co2"]))
    b = _id_of(handler.put(text="beta", tags=["confidence-strong"]))
    new_id = _new_id(handler.supersede(merge_ids=[a, b], new_text="ab"))
    tags = {str(t) for t in store.tags_for(new_id)}
    assert {"topic:co2", "confidence-strong", "DREAM:consolidated"} <= tags


def test_supersede_survivor_keeps_the_originals_space(
    handler: MemoryHandler, store: Store
) -> None:
    a = _id_of(handler.put(text="alpha", tags=["SPACE:repo-dev"]))
    b = _id_of(handler.put(text="beta", tags=["SPACE:repo-dev"]))
    new_id = _new_id(handler.supersede(merge_ids=[a, b], new_text="ab"))
    spaces = {str(t) for t in store.tags_for(new_id) if t.prefix == "SPACE"}
    assert spaces == {"SPACE:repo-dev"}


def test_supersede_refuses_a_merge_across_spaces(handler: MemoryHandler) -> None:
    a = _id_of(handler.put(text="alpha", tags=["SPACE:repo-dev"]))
    b = _id_of(handler.put(text="beta"))  # default SPACE:research
    with pytest.raises(BadInput, match="across spaces"):
        handler.supersede(merge_ids=[a, b], new_text="ab")


def test_supersede_requires_two_ids(handler: MemoryHandler) -> None:
    a = _id_of(handler.put(text="solo"))
    with pytest.raises(BadInput, match="2"):
        handler.supersede(merge_ids=[a], new_text="x")
    with pytest.raises(BadInput, match="2 distinct"):
        handler.supersede(merge_ids=[a, a], new_text="x")


def test_supersede_rejects_non_memory(handler: MemoryHandler, store: Store) -> None:
    a = _id_of(handler.put(text="a real memory"))
    todo = store.insert_ref(kind="todo", slug=None, title="not a memory", meta={})
    with pytest.raises(BadInput, match="not a live memory"):
        handler.supersede(merge_ids=[a, todo.id], new_text="x")


def test_supersede_is_compress_only(handler: MemoryHandler) -> None:
    a = _id_of(handler.put(text="short"))
    b = _id_of(handler.put(text="tiny"))
    with pytest.raises(BadInput, match="compress-only"):
        handler.supersede(merge_ids=[a, b], new_text="x" * 200)


def test_supersede_caps_merge_count(handler: MemoryHandler) -> None:
    ids = [_id_of(handler.put(text=f"dup {i}")) for i in range(11)]
    with pytest.raises(BadInput, match="caps at 10"):
        handler.supersede(merge_ids=ids, new_text="merged")


# ---------------------------------------------------------------------------
# Salience: memory search heats its card chunk (dreaming target signal)
# ---------------------------------------------------------------------------


def _card_last_seen(store: Store, ref_id: int) -> datetime:
    cids = store.chunks.card_chunk_ids([ref_id])
    assert len(cids) == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_seen FROM chunks WHERE chunk_id = %s", (cids[0],)
        ).fetchone()
    assert row is not None
    return row[0]


def test_memory_search_bumps_card_salience(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _id_of(handler.put(text="copper nitrate reduction pathway"))
    before = _card_last_seen(store, mid)
    handler.search(q="copper nitrate")
    after = _card_last_seen(store, mid)
    assert after > before


def test_memory_search_does_not_bump_for_dream_actor(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _id_of(handler.put(text="palladium hydride storage capacity"))
    before = _card_last_seen(store, mid)
    with as_dream_actor():
        handler.search(q="palladium hydride")
    after = _card_last_seen(store, mid)
    assert after == before


# ---------------------------------------------------------------------------
# Through the runtime dispatcher
# ---------------------------------------------------------------------------


def test_runtime_create_memory(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("put", {"kind": "memory", "text": "via dispatch"})
    assert "created memory me" in out


def test_runtime_create_then_get(runtime_with_store: PrecisRuntime) -> None:
    create = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "round trip"}
    )
    new_id = id_of(create)

    got = runtime_with_store.dispatch("get", {"kind": "memory", "id": new_id})
    assert "round trip" in got


def test_runtime_search_memory(runtime_with_store: PrecisRuntime) -> None:
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kwargs vs modes decision"}
    )
    out = runtime_with_store.dispatch("search", {"kind": "memory", "q": "kwargs"})
    assert "kwargs" in out


def test_runtime_unknown_memory_renders_error(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": 99999})
    assert "[error:NotFound]" in out
    assert "next:" in out


# ---------------------------------------------------------------------------
# meta={'hook': ...} — the index line of a SPACE:repo-dev memory
# ---------------------------------------------------------------------------


def test_put_meta_hook_is_stored(handler: MemoryHandler, store: Store) -> None:
    mid = id_of(handler.put(text="body", meta={"hook": "  one line  "}).body)
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "one line"


def test_edit_meta_only_sets_the_hook(handler: MemoryHandler, store: Store) -> None:
    mid = id_of(handler.put(text="body stays").body)
    out = handler.edit(id=mid, meta={"hook": "now hooked"})
    assert "meta: hook" in out.body
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    assert ref.meta["hook"] == "now hooked"
    assert handler._body_text(ref) == "body stays"


def test_edit_meta_type_is_validated(handler: MemoryHandler, store: Store) -> None:
    mid = id_of(handler.put(text="body").body)
    handler.edit(id=mid, meta={"type": "reference"})
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["type"] == "reference"
    with pytest.raises(BadInput, match="meta\\['type'\\]"):
        handler.edit(id=mid, meta={"type": "bogus"})


def test_edit_meta_with_text_rewrites_both(
    handler: MemoryHandler, store: Store
) -> None:
    mid = id_of(handler.put(text="v1", meta={"hook": "old"}).body)
    handler.edit(id=mid, text="v2", meta={"hook": "new"})
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    assert (handler._body_text(ref), ref.meta["hook"]) == ("v2", "new")


@pytest.mark.parametrize(
    "bad",
    [
        {"slug": "x"},
        {"hook": "ok", "order": 3},
        {"hook": 5},
        {"hook": "two\nlines"},
        {"hook": "   "},
        {"hook": ""},
        "not-a-dict",
    ],
)
def test_bad_meta_raises_on_put_and_edit(
    handler: MemoryHandler, store: Store, bad: Any
) -> None:
    mid = id_of(handler.put(text="victim").body)
    with pytest.raises(BadInput):
        handler.put(text="never created", meta=bad)
    with pytest.raises(BadInput):
        handler.edit(id=mid, meta=bad)
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "hook" not in ref.meta
    assert len(store.list_refs(kind="memory", limit=100)) == 1


def test_bad_meta_key_names_the_allowed_keys(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match=r"allowed keys: \['hook', 'type'\]"):
        handler.put(text="x", meta={"nope": "y"})


def test_edit_with_nothing_to_change_names_meta(handler: MemoryHandler) -> None:
    mid = id_of(handler.put(text="x").body)
    with pytest.raises(BadInput, match="text=, rule=, warrant=, or meta="):
        handler.edit(id=mid)
    with pytest.raises(BadInput, match="text=, rule=, warrant=, or meta="):
        handler.edit(id=mid, meta={})


def test_runtime_put_and_edit_route_meta_to_the_handler(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    # The verb layer sends put's meta= as a flat key and edit's through the
    # ``__extras__`` channel (tools/core.py); both reach the handler.
    created = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "via the verb layer",
            "tags": ["SPACE:repo-dev"],
            "meta": {"hook": "put hook"},
        },
    )
    mid = id_of(created)
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "put hook"

    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "replace",
            "__extras__": {"meta": {"hook": "edit hook"}},
        },
    )
    assert "[error" not in out and "meta: hook" in out
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "edit hook"

    bad = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "replace",
            "__extras__": {"meta": {"slug": "x"}},
        },
    )
    assert "[error:BadInput]" in bad and "allowed keys" in bad


def test_kindspec_supports_get_search_put(runtime_with_store: PrecisRuntime) -> None:
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None
    assert handler.spec.is_numeric is True
    assert handler.spec.supports_get is True
    assert handler.spec.supports_search is True
    assert handler.spec.supports_put is True
    assert handler.spec.supports_delete is True
    assert handler.spec.supports_tag is True
    assert handler.spec.supports_link is True


# ---------------------------------------------------------------------------
# edit(mode='find-replace' | 'insert') — anchored body edits
# ---------------------------------------------------------------------------

_BODY = "alpha path /old/dir\nbeta line\ngamma tail"


def _make(handler: MemoryHandler, text: str = _BODY) -> int:
    return id_of(handler.put(text=text, title="anchored").body)


def _body(handler: MemoryHandler, store: Store, mid: int) -> str:
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    return handler._body_text(ref)


def test_find_replace_unique_hit(handler: MemoryHandler, store: Store) -> None:
    mid = _make(handler)
    out = handler.edit(id=mid, mode="find-replace", find="/old/dir", text="/new/dir")
    assert f"edited body of memory id={mid}" in out.body
    assert _body(handler, store, mid) == _BODY.replace("/old/dir", "/new/dir")
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.title == "anchored"


def test_find_replace_ambiguous_raises_with_candidates(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler, "foo one\nfoo two\nbar")
    with pytest.raises(BadInput, match=r"2 matches") as exc:
        handler.edit(id=mid, mode="find-replace", find="foo", text="baz")
    assert "L1" in str(exc.value) and "L2" in str(exc.value)
    assert _body(handler, store, mid) == "foo one\nfoo two\nbar"


def test_find_replace_not_found_raises(handler: MemoryHandler) -> None:
    mid = _make(handler)
    with pytest.raises(BadInput, match="not found"):
        handler.edit(id=mid, mode="find-replace", find="nonexistent", text="x")


def test_find_replace_match_all(handler: MemoryHandler, store: Store) -> None:
    mid = _make(handler, "foo one\nfoo two\nbar")
    handler.edit(id=mid, mode="find-replace", find="foo", text="baz", match="all")
    assert _body(handler, store, mid) == "baz one\nbaz two\nbar"


def test_find_replace_match_first_nth_and_anchors(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler, "foo one\nfoo two\nfoo three")
    handler.edit(id=mid, mode="find-replace", find="foo", text="X", match="first")
    assert _body(handler, store, mid) == "X one\nfoo two\nfoo three"
    handler.edit(id=mid, mode="find-replace", find="foo", text="Y", match="nth", nth=2)
    assert _body(handler, store, mid) == "X one\nfoo two\nY three"
    handler.edit(id=mid, mode="find-replace", find="foo", text="Z", after=" two")
    assert _body(handler, store, mid) == "X one\nZ two\nY three"


@pytest.mark.parametrize(
    ("where", "text", "expected"),
    [
        ("before", "NEW ", "alpha NEW path /old/dir\nbeta line\ngamma tail"),
        ("after", " NEW", "alpha path NEW /old/dir\nbeta line\ngamma tail"),
    ],
)
def test_insert_before_after(
    handler: MemoryHandler, store: Store, where: str, text: str, expected: str
) -> None:
    mid = _make(handler)
    out = handler.edit(id=mid, mode="insert", find="path", text=text, where=where)
    assert f"inserted into body of memory id={mid}" in out.body
    assert _body(handler, store, mid) == expected


def test_insert_requires_where(handler: MemoryHandler) -> None:
    mid = _make(handler)
    with pytest.raises(BadInput, match="where="):
        handler.edit(id=mid, mode="insert", find="beta", text="x")


def test_empty_text_deletes_the_matched_span(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    handler.edit(id=mid, mode="find-replace", find="beta line\n", text="")
    assert _body(handler, store, mid) == "alpha path /old/dir\ngamma tail"


def test_edit_yielding_empty_body_is_bad_input(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler, "only this")
    with pytest.raises(BadInput, match="empty body"):
        handler.edit(id=mid, mode="find-replace", find="only this", text="")
    assert _body(handler, store, mid) == "only this"


def test_dry_run_writes_nothing(handler: MemoryHandler, store: Store) -> None:
    mid = _make(handler)
    before_ids = [c.id for c in store.chunks.list_chunks_for_ref(mid)]
    out = handler.edit(
        id=mid, mode="find-replace", find="/old/dir", text="/new/dir", dry_run=True
    )
    assert "DRY RUN" in out.body
    assert "-alpha path /old/dir" in out.body
    assert "+alpha path /new/dir" in out.body
    assert _body(handler, store, mid) == _BODY
    assert [c.id for c in store.chunks.list_chunks_for_ref(mid)] == before_ids
    with store.tx() as conn:
        events = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id=%s AND event='body_replaced'",
            (mid,),
        ).fetchone()
    assert events is not None and events[0] == 0


def test_dry_run_full_mode(handler: MemoryHandler, store: Store) -> None:
    mid = _make(handler)
    out = handler.edit(
        id=mid, mode="find-replace", find="beta", text="BETA", dry_run="full"
    )
    assert "BETA line" in out.body
    assert _body(handler, store, mid) == _BODY


def test_dry_run_on_replace_mode_is_rejected(handler: MemoryHandler) -> None:
    mid = _make(handler)
    with pytest.raises(BadInput, match="dry_run"):
        handler.edit(id=mid, mode="replace", text="whole new", dry_run=True)


def test_omitted_mode_with_text_only_still_replaces_whole_body(
    handler: MemoryHandler, store: Store
) -> None:
    # Direct handler call: mode defaults to 'replace'.
    mid = _make(handler)
    handler.edit(id=mid, text="entirely new body")
    assert _body(handler, store, mid) == "entirely new body"


def test_mcp_default_find_replace_with_text_only_is_refused(
    handler: MemoryHandler, store: Store
) -> None:
    # The verb layer defaults mode to 'find-replace'; text= with no find=
    # must not silently overwrite the body.
    mid = _make(handler)
    with pytest.raises(BadInput, match="find=") as exc:
        handler.edit(id=mid, mode="find-replace", text="oops")
    assert "mode='replace'" in (exc.value.next or "")
    assert _body(handler, store, mid) == _BODY


def test_mcp_default_find_replace_with_meta_only_patches_meta(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    handler.edit(id=mid, mode="find-replace", meta={"hook": "via default mode"})
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "via default mode"
    assert handler._body_text(ref) == _BODY


def test_anchored_edit_takes_meta_but_rejects_replace_only_fields(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    handler.edit(
        id=mid,
        mode="find-replace",
        find="beta",
        text="BETA",
        meta={"hook": "with body edit"},
    )
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "with body edit"
    for bad in ({"title": "t"}, {"rule": "r"}, {"warrant": "w"}):
        kwargs: dict[str, Any] = {"find": "BETA", "text": "x", **bad}
        with pytest.raises(BadInput, match="replace-only"):
            handler.edit(id=mid, mode="find-replace", **kwargs)
    assert "BETA" in _body(handler, store, mid)


def test_anchored_edit_records_old_body_and_reinserts_chunk(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    old_chunks = store.chunks.list_chunks_for_ref(mid)
    handler.edit(id=mid, mode="find-replace", find="/old/dir", text="/new/dir")
    new_chunks = store.chunks.list_chunks_for_ref(mid)
    new_text = _BODY.replace("/old/dir", "/new/dir")
    assert [c.text for c in new_chunks] == [new_text]
    # delete + insert, never an in-place UPDATE: a fresh chunk row.
    assert {c.id for c in new_chunks}.isdisjoint({c.id for c in old_chunks})
    assert new_chunks[0].chunk_kind == "memory_body"
    with store.tx() as conn:
        row = conn.execute(
            "SELECT payload FROM ref_events "
            "WHERE ref_id=%s AND event='body_replaced' ORDER BY 1 DESC LIMIT 1",
            (mid,),
        ).fetchone()
    assert row is not None
    assert row[0]["old_text"] == _BODY
    assert row[0]["new_text"] == new_text


def test_runtime_edit_default_mode_find_replace_reaches_handler(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    created = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "one stale path here"}
    )
    mid = id_of(created)
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "find-replace",
            "find": "stale",
            "text": "fresh",
        },
    )
    assert "[error" not in out, out
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    handler = runtime_with_store.hub.handler_for("memory")
    assert isinstance(handler, MemoryHandler)
    assert handler._body_text(ref) == "one fresh path here"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "insert"},
        {"mode": "find-replace", "text": ""},
        {"mode": "find-replace", "text": "   "},
    ],
)
def test_incomplete_anchored_edit_cannot_patch_meta(
    handler: MemoryHandler, store: Store, kwargs: dict[str, Any]
) -> None:
    mid = _make(handler)
    with pytest.raises(BadInput, match="requires find="):
        handler.edit(id=mid, meta={"hook": "must not land"}, **kwargs)
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "hook" not in ref.meta
    assert _body(handler, store, mid) == _BODY


def test_tool_default_mode_edits_body_and_meta(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    handler = MemoryHandler(hub=runtime_with_store.hub)
    mid = _make(handler)
    out = core.edit(kind="memory", id=mid, find="beta", text="BETA")
    assert isinstance(out, str) and "edited body" in out
    assert "BETA line" in _body(handler, store, mid)
    out = core.edit(kind="memory", id=mid, meta={"hook": "new hook"})
    assert isinstance(out, str) and "updated memory" in out
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.meta["hook"] == "new hook"


def test_anchored_dry_run_does_not_patch_meta(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    handler.edit(
        id=mid,
        mode="insert",
        find="beta",
        text="NEW ",
        where="before",
        dry_run=True,
        meta={"hook": "must not land"},
    )
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "hook" not in ref.meta
    assert _body(handler, store, mid) == _BODY


def test_stale_anchored_edit_rolls_back_body_meta_events_and_derived_rows(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = _make(handler)
    read_body = handler._body_text
    winner_body = _BODY + "\nconcurrent addition"
    winner_chunk_ids: list[int] = []

    def read_then_commit_other_edit(ref: Any) -> str:
        snapshot = read_body(ref)
        handler.edit(id=mid, mode="replace", text=winner_body)
        chunk_id = store.chunks.list_chunks_for_ref(mid)[0].id
        winner_chunk_ids.append(chunk_id)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO chunk_summaries (chunk_id, summarizer, text) "
                "VALUES (%s, 'llm-v1', 'winner summary')",
                (chunk_id,),
            )
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder) "
                "SELECT %s, name FROM embedders WHERE is_default LIMIT 1",
                (chunk_id,),
            )
        return snapshot

    monkeypatch.setattr(handler, "_body_text", read_then_commit_other_edit)
    with pytest.raises(BadInput, match="changed during the edit"):
        handler.edit(
            id=mid,
            mode="find-replace",
            find="beta",
            text="BETA",
            meta={"hook": "loser"},
        )
    monkeypatch.setattr(handler, "_body_text", read_body)
    assert _body(handler, store, mid) == winner_body
    assert [c.id for c in store.chunks.list_chunks_for_ref(mid)] == winner_chunk_ids
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "hook" not in ref.meta
    with store.tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id=%s AND event='body_replaced'",
            (mid,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT text FROM chunk_summaries WHERE chunk_id=%s",
            (winner_chunk_ids[0],),
        ).fetchone() == ("winner summary",)
        assert conn.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE chunk_id=%s",
            (winner_chunk_ids[0],),
        ).fetchone() == (1,)

    # A fresh retry removes old derived rows; workers fill the new chunk.
    handler.edit(id=mid, mode="find-replace", find="beta", text="BETA")
    assert _body(handler, store, mid) == winner_body.replace("beta", "BETA")
    with store.tx() as conn:
        for table in ("chunk_summaries", "chunk_embeddings"):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE chunk_id=%s",
                (winner_chunk_ids[0],),
            ).fetchone() == (0,)


def test_competing_anchored_edits_have_one_winner_and_one_retry(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = _make(handler)
    both_read = Barrier(2, timeout=10)
    read_body = handler._body_text

    def synchronized_read(ref: Any) -> str:
        snapshot = read_body(ref)
        both_read.wait()
        return snapshot

    monkeypatch.setattr(handler, "_body_text", synchronized_read)

    def edit(replacement: str) -> str:
        try:
            handler.edit(id=mid, mode="find-replace", find="beta", text=replacement)
        except BadInput as exc:
            assert "changed during the edit" in str(exc)
            return "retry"
        return replacement

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["FIRST", "SECOND"]))
    monkeypatch.setattr(handler, "_body_text", read_body)
    assert results.count("retry") == 1
    winner = next(result for result in results if result != "retry")
    assert _body(handler, store, mid) == _BODY.replace("beta", winner)


def test_anchored_edit_after_retirement_cannot_write(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = _make(handler)
    read_body = handler._body_text

    def read_then_retire(ref: Any) -> str:
        snapshot = read_body(ref)
        handler.delete(id=mid)
        return snapshot

    monkeypatch.setattr(handler, "_body_text", read_then_retire)
    with pytest.raises(Gone):
        handler.edit(id=mid, mode="find-replace", find="beta", text="BETA")
    assert [c.text for c in store.chunks.list_chunks_for_ref(mid)] == [_BODY]


def test_anchored_edit_resyncs_mentions_and_preserves_manual_links(
    handler: MemoryHandler, store: Store
) -> None:
    old = _make(handler, "old target")
    new = _make(handler, "new target")
    manual = _make(handler, "manual target")
    mid = _make(handler, f"see memory:{old}")
    store.add_link(src_ref_id=mid, dst_ref_id=manual, relation="related-to")
    handler.edit(
        id=mid, mode="find-replace", find=f"memory:{old}", text=f"memory:{new}"
    )
    links = store.links_for(mid, direction="out", relation="related-to")
    assert {link.dst_ref_id for link in links} == {new, manual}
    assert next(link for link in links if link.dst_ref_id == new).meta == {
        "auto": "mention"
    }


def test_anchored_edit_of_legacy_title_only_memory(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _make(handler)
    with store.tx() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id=%s", (mid,))
    handler.edit(id=mid, mode="find-replace", find="anchored", text="new body")
    assert _body(handler, store, mid) == "new body"


@pytest.mark.parametrize("mode", ["replace", "find-replace"])
def test_body_writes_hold_ref_lock_before_store_read_and_replacement(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from psycopg.errors import LockNotAvailable

    mid = _make(handler)
    replace_body = store.chunks.replace_body_chunk

    def assert_locked(*args: Any, **kwargs: Any) -> str | None:
        # A separate live PG transaction must fail before the store even
        # reads the old chunk. This detects removal/misplacement of the lock.
        with pytest.raises(LockNotAvailable), store.tx() as competitor:
            competitor.execute(
                "SELECT ref_id FROM refs WHERE ref_id=%s FOR UPDATE NOWAIT", (mid,)
            )
        return replace_body(*args, **kwargs)

    monkeypatch.setattr(store.chunks, "replace_body_chunk", assert_locked)
    kwargs: dict[str, Any] = {"find": "beta"} if mode == "find-replace" else {}
    handler.edit(id=mid, mode=mode, text="new prose", **kwargs)


@pytest.mark.parametrize("second_fails", [False, True])
def test_shared_runtime_put_keeps_creation_fields_request_local(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    second_fails: bool,
) -> None:
    from precis.handlers import _link_target

    runtime = runtime_with_store
    target = id_of(runtime.dispatch("put", {"kind": "memory", "text": "target"}))
    link = f"memory:{target}"
    first_resolving = Event()
    second_finished = Event()
    parse_target = _link_target.parse_link_target

    def pause_first(value: str, **kwargs: Any) -> Any:
        if value == link:
            first_resolving.set()
            assert second_finished.wait(10), "second put did not finish"
        return parse_target(value, **kwargs)

    monkeypatch.setattr(_link_target, "parse_link_target", pause_first)

    def payload(label: str) -> dict[str, Any]:
        return {
            "kind": "memory",
            "text": f"body {label}",
            "title": f"title {label}",
            "rule": f"rule {label}",
            "warrant": f"warrant {label}",
            "meta": {"hook": f"hook {label}"},
        }

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.dispatch, "put", {**payload("A"), "link": link})
        try:
            assert first_resolving.wait(10), "first put did not reach link resolution"
            second_payload = payload("B")
            if second_fails:
                second_payload["text"] = ""
            second = runtime.dispatch("put", second_payload)
        finally:
            second_finished.set()
        first_result = first.result(timeout=10)

    results = [("A", first_result)]
    if second_fails:
        assert "[error:BadInput]" in second
    else:
        results.append(("B", second))
    for label, result in results:
        ref = store.get_ref(kind="memory", id=id_of(result))
        assert ref is not None and ref.title == f"title {label}"
        assert ref.meta == {
            "rule": f"rule {label}",
            "warrant": f"warrant {label}",
            "hook": f"hook {label}",
        }


@pytest.mark.parametrize("mode", ["replace", "find-replace"])
def test_reciprocal_memory_edits_persist_bodies_events_and_mentions(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    first, second = _make(handler, "first"), _make(handler, "second")
    before_mentions = Barrier(2, timeout=10)
    sync_mentions = handler._sync_mention_links

    def synchronized_mentions(
        ref_id: int, text: str, *, conn: Any, replace: bool = False
    ) -> int:
        conn.execute("SET LOCAL statement_timeout = '5s'")
        before_mentions.wait()
        return sync_mentions(ref_id, text, conn=conn, replace=replace)

    monkeypatch.setattr(handler, "_sync_mention_links", synchronized_mentions)
    bodies = {
        first: f"first mentions memory:{second}",
        second: f"second mentions memory:{first}",
    }

    def edit(mid: int) -> Response:
        kwargs: dict[str, Any] = (
            {"find": "first" if mid == first else "second"}
            if mode == "find-replace"
            else {}
        )
        return handler.edit(id=mid, mode=mode, text=bodies[mid], **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [first, second]))
    assert all("body of memory" in result.body for result in results)
    for mid, target in ((first, second), (second, first)):
        assert _body(handler, store, mid) == bodies[mid]
        links = store.links_for(mid, direction="out", relation="related-to")
        assert [link.dst_ref_id for link in links] == [target]
        with store.tx() as conn:
            assert conn.execute(
                "SELECT payload->>'new_text' FROM ref_events "
                "WHERE ref_id=%s AND event='body_replaced'",
                (mid,),
            ).fetchall() == [(bodies[mid],)]


@pytest.mark.parametrize("mode", ["put", "replace", "find-replace"])
def test_mention_sql_failure_cannot_return_success(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    mid, target = _make(handler), _make(handler, "target")
    old_ids = [chunk.id for chunk in store.chunks.list_chunks_for_ref(mid)]
    with store.tx() as conn:
        ref_count = conn.execute("SELECT count(*) FROM refs").fetchone()

    def sql_failure(**kwargs: Any) -> None:
        # Runs on the real caller connection inside the helper's catch-all;
        # a plain Python exception would not put PostgreSQL in INERROR.
        kwargs["conn"].execute("SELECT 1 / 0")

    monkeypatch.setattr(store, "add_link", sql_failure)
    body = f"new body mentioning memory:{target}"
    with pytest.raises(Internal, match="mention.*transaction"):
        if mode == "put":
            handler.put(text=body, meta={"hook": "must not land"})
        else:
            kwargs: dict[str, Any] = {"find": _BODY} if mode == "find-replace" else {}
            handler.edit(
                id=mid, mode=mode, text=body, meta={"hook": "must not land"}, **kwargs
            )
    assert _body(handler, store, mid) == _BODY
    assert [chunk.id for chunk in store.chunks.list_chunks_for_ref(mid)] == old_ids
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "hook" not in ref.meta
    with store.tx() as conn:
        assert conn.execute("SELECT count(*) FROM refs").fetchone() == ref_count
        assert conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id=%s AND event='body_replaced'",
            (mid,),
        ).fetchone() == (0,)


def test_memory_dogfood_counts_body_across_authoring_modes(
    handler: MemoryHandler, store: Store
) -> None:
    tag = "project:r6-body-count-regression"
    body = "alpha beta gamma delta"
    mid = id_of(handler.put(text=body, title="Brief title", tags=[tag]).body)

    def assert_count(expected: str) -> None:
        assert store.chunks.chunk_word_counts([mid], chunk_kind="memory_body") == {
            mid: len(expected.split())
        }
        rows = handler.search(tags=[tag]).body.splitlines()
        row = next(line for line in rows if f"me{mid}" in line)
        assert row.split("\t")[3] == str(len(expected.split()))

    assert_count(body)
    body = "silver sails across quiet harbors"
    handler.edit(id=mid, mode="replace", text=body)
    assert_count(body)
    handler.edit(id=mid, mode="find-replace", find="quiet", text="clear sunlit")
    body = body.replace("quiet", "clear sunlit")
    assert_count(body)
    handler.edit(id=mid, mode="insert", find="harbors", where="before", text="safe ")
    body = body.replace("harbors", "safe harbors")
    assert_count(body)


def test_memory_dogfood_existing_body_count_is_read_only(
    handler: MemoryHandler, store: Store
) -> None:
    body = "Sails cross seas; seven vessels sail safely."
    mid = _make(handler, body)
    ref_before = store.get_ref(kind="memory", id=mid)
    chunks_before = store.chunks.list_chunks_for_ref(mid)
    with store.tx() as conn:
        events_before = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id=%s", (mid,)
        ).fetchone()
        # Confirm the escaping defect rather than assuming the title was read.
        pattern = conn.execute("SELECT E'\\s+'").fetchone()
        assert pattern == ("s+",)
    assert store.chunks.chunk_word_counts([mid], chunk_kind="memory_body") == {mid: 7}
    assert store.get_ref(kind="memory", id=mid) == ref_before
    assert store.chunks.list_chunks_for_ref(mid) == chunks_before
    with store.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM ref_events WHERE ref_id=%s", (mid,)
            ).fetchone()
            == events_before
        )


def test_memory_dogfood_counts_whitespace_and_only_body_chunks(
    handler: MemoryHandler, store: Store
) -> None:
    from precis.store.types import ChunkInsert

    mid = _make(handler, " \tgo\tto\nshore  ")
    store.chunks.insert_chunks(
        mid,
        [
            ChunkInsert(ord=1, text=" \t\n ", meta={"chunk_kind": "memory_body"}),
            ChunkInsert(
                ord=2, text=" sails safely ", meta={"chunk_kind": "memory_body"}
            ),
            ChunkInsert(
                ord=3,
                text="ignore these extra words",
                meta={"chunk_kind": "tag_overflow"},
            ),
        ],
    )
    assert store.chunks.chunk_word_counts([], chunk_kind="memory_body") == {}
    assert store.chunks.chunk_word_counts([mid], chunk_kind="memory_body") == {mid: 5}


def test_memory_dogfood_missing_anchor_hint_performs_insert(
    handler: MemoryHandler, store: Store
) -> None:
    import ast

    mid = _make(handler, "Before exact text after.")
    with pytest.raises(BadInput, match="requires find=") as exc:
        handler.edit(id=mid, mode="insert", meta={"hook": "must not land"})
    assert isinstance(exc.value.next, str)
    call = ast.parse(exc.value.next, mode="eval").body
    assert isinstance(call, ast.Call)
    kwargs = {}
    for kw in call.keywords:
        assert kw.arg is not None
        kwargs[kw.arg] = ast.literal_eval(kw.value)
    assert kwargs.pop("kind") == "memory"
    assert kwargs["mode"] == "insert" and kwargs["where"] in ("before", "after")
    assert kwargs["id"] == mid
    handler.edit(**kwargs)
    anchor, addition = kwargs["find"], kwargs["text"]
    replacement = anchor + addition if kwargs["where"] == "after" else addition + anchor
    assert _body(handler, store, mid) == "Before exact text after.".replace(
        anchor, replacement
    )


# ---------------------------------------------------------------------------
# The memory walk: view='fisheye…' (extent ladder), memory-graph slice 1b
# ---------------------------------------------------------------------------


def _mirrored(store: Store, title: str, filename: str | None, *tags: str) -> int:
    from precis.store import Tag

    ref = store.insert_ref(kind="memory", slug=None, title=title)
    if filename is not None:
        store.stamp_ref_meta(ref.id, {"file_mirror": {"filename": filename}})
    for t in tags:
        store.add_tag(ref.id, Tag.parse_strict(t, kind="memory"), set_by="agent")
    return int(ref.id)


def _walk_fixture(hub: Hub) -> tuple[int, int, int, int]:
    store = hub.live_store
    focus = _mirrored(store, "Worker busy", "worker_busy.md", "SPACE:repo-dev")
    section = _mirrored(store, "Worker section", "worker_section.md")
    sib = _mirrored(store, "Sibling note", "sibling.md")
    far = _mirrored(store, "Far note", None)
    store.add_link(src_ref_id=focus, dst_ref_id=section, relation="part-of")
    store.add_link(src_ref_id=focus, dst_ref_id=sib, relation="related-to")
    store.add_link(src_ref_id=sib, dst_ref_id=far, relation="related-to")
    return focus, section, sib, far


def test_fisheye_1hop_renders_rings_with_filenames(
    hub: Hub, handler: MemoryHandler
) -> None:
    focus, section, sib, _far = _walk_fixture(hub)
    out = handler.get(id=focus, view="fisheye+1hop").body
    assert out.startswith(f"me{focus} (worker_busy.md) [memory] Worker busy")
    parts = out.split("Parts:")[1].split("Notes & links:")[0]
    assert f"me{section} (worker_section.md) — Worker section" in parts
    notes = out.split("Notes & links:")[1]
    assert f"related-to: me{sib} (sibling.md) — Sibling note" in notes


def test_fisheye_2hop_renders_counts(hub: Hub, handler: MemoryHandler) -> None:
    focus, *_ = _walk_fixture(hub)
    out = handler.get(id=focus, view="fisheye+2hop").body
    assert "— second hop (2 neighbours out" in out
    assert "  1 memory via related-to" in out


def test_fisheye_plain_rung_still_dispatches(hub: Hub, handler: MemoryHandler) -> None:
    focus, *_ = _walk_fixture(hub)
    out = handler.get(id=focus, view="fisheye").body
    assert "— linked (1 hop) —" not in out
    assert f"me{focus} (worker_busy.md)" in out


def test_memory_invalid_view_lists_the_rungs(hub: Hub, handler: MemoryHandler) -> None:
    from precis.errors import Unsupported

    focus, *_ = _walk_fixture(hub)
    with pytest.raises(Unsupported) as ei:
        handler.get(id=focus, view="fisheye+9hop")
    msg = str(ei.value) + " " + str(getattr(ei.value, "next", ""))
    for rung in (
        "kwd",
        "summary",
        "verbatim",
        "fisheye",
        "fisheye+1hop",
        "fisheye+2hop",
    ):
        assert rung in msg
    with pytest.raises(BadInput):
        handler.get(id=focus, view="fisheye+9hop+recall")


def _embedded_memory(rt: PrecisRuntime, text: str, space: str) -> int:
    out = rt.dispatch("put", {"kind": "memory", "text": text, "tags": [space]})
    ref_id = id_of(out)
    store = rt.hub.live_store
    (cid,) = store.chunks.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, rt.hub.embed_one(text)),
        )
    return ref_id


def test_recall_stays_inside_the_focus_space(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    text = "copper catalyses nitrate reduction near-identical body"
    dev_a = _embedded_memory(rt, text, "SPACE:repo-dev")
    dev_b = _embedded_memory(rt, text, "SPACE:repo-dev")
    res = _embedded_memory(rt, text, "SPACE:research")
    h = MemoryHandler(hub=rt.hub)
    dev_out = h.get(id=dev_a, view="+recall").body.split("— recall")[1]
    assert f"me{dev_b}" in dev_out
    assert f"me{res}" not in dev_out
    res_out = h.get(id=res, view="fisheye+1hop+recall").body.split("— recall")[1]
    assert f"me{dev_a}" not in res_out and f"me{dev_b}" not in res_out


# ---------------------------------------------------------------------------
# Recall: search(view='index') — the index line is the hit (slice 2)
# ---------------------------------------------------------------------------


def _put_repo_dev(
    handler: MemoryHandler,
    store: Store,
    text: str,
    title: str,
    *,
    hook: str | None = None,
    filename: str | None = None,
) -> int:
    meta = {"hook": hook} if hook else None
    out = handler.put(text=text, title=title, tags=["SPACE:repo-dev"], meta=meta)
    ref_id = id_of(out.body)
    if filename:
        store.stamp_ref_meta(ref_id, {"file_mirror": {"filename": filename}})
    return ref_id


def test_search_view_index_renders_handle_filename_and_hook(
    handler: MemoryHandler, store: Store
) -> None:
    a = _put_repo_dev(
        handler,
        store,
        "The quokka scheduler stalls when heartbeat lapses.",
        "Quokka stall",
        hook="restart the quokka scheduler",
        filename="quokka_stall.md",
    )
    b = _put_repo_dev(
        handler,
        store,
        "Native quokka note\nsecond line",
        "Quokka native",
    )
    out = handler.search(q="quokka", tags=["SPACE:repo-dev"], view="index").body
    lines = out.splitlines()
    assert (
        f"- Quokka stall (me{a}, quokka_stall.md) — restart the quokka scheduler"
        in lines
    )
    # no hook, no mirror filename: first body line stands in for the hook
    assert f"- Quokka native (me{b}) — Native quokka note" in lines
    assert all(ln.startswith("- ") for ln in lines)


def test_search_view_index_is_refused_for_other_views_and_empty_q(
    handler: MemoryHandler,
) -> None:
    with pytest.raises(BadInput) as ei:
        handler.search(q="x", view="nope")
    assert "index" in str(ei.value) + str(getattr(ei.value, "options", ""))
    with pytest.raises(BadInput):
        handler.search(view="index", tags=["SPACE:repo-dev"])


def test_search_default_view_is_unchanged(handler: MemoryHandler, store: Store) -> None:
    _put_repo_dev(handler, store, "zebra body text", "Zebra title", hook="zebra hook")
    default = handler.search(q="zebra", tags=["SPACE:repo-dev"]).body
    assert default == handler.search(q="zebra", tags=["SPACE:repo-dev"], view=None).body
    assert "zebra hook" not in default
    assert "Zebra title" in default


@pytest.mark.parametrize("mode", ["put", "replace", "find-replace", "insert"])
@pytest.mark.parametrize("driver_error", [False, True])
def test_mention_connection_loss_cannot_return_success(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    driver_error: bool,
) -> None:
    from psycopg import Connection, connect
    from psycopg.pq import TransactionStatus

    old_target, new_target = _make(handler, "old target"), _make(handler, "new target")
    manual = _make(handler, "manual target")
    old_body = f"original memory:{old_target}"
    mid = id_of(
        handler.put(
            text=old_body, title="original title", meta={"hook": "original hook"}
        ).body
    )
    store.add_link(src_ref_id=mid, dst_ref_id=manual, relation="related-to")
    dsn = store.dsn
    assert dsn is not None

    def persisted_state() -> list[Any]:
        # Open a fresh physical connection each time, never the lost caller
        # connection or a cached object. Include identities and metadata so
        # both removal of old mentions and insertion of new ones must undo.
        with connect(dsn) as fresh:
            return [
                fresh.execute(
                    "SELECT ref_id, title, meta FROM refs ORDER BY ref_id"
                ).fetchall(),
                fresh.execute(
                    "SELECT chunk_id, ref_id, text FROM chunks ORDER BY chunk_id"
                ).fetchall(),
                fresh.execute(
                    "SELECT event_id, ref_id, event, payload FROM ref_events ORDER BY event_id"
                ).fetchall(),
                fresh.execute(
                    "SELECT link_id, src_ref_id, dst_ref_id, relation, meta FROM links ORDER BY link_id"
                ).fetchall(),
            ]

    before = persisted_state()
    add_link = store.add_link
    closed: list[Connection] = []

    def insert_then_disconnect(**kwargs: Any) -> Any:
        result = add_link(**kwargs)
        if kwargs["dst_ref_id"] == new_target:
            conn = kwargs["conn"]
            assert conn.info.transaction_status == TransactionStatus.INTRANS
            conn.close()
            closed.append(conn)
            if driver_error:
                conn.execute("SELECT 1")  # real driver error caught by shared helper
        return result

    monkeypatch.setattr(store, "add_link", insert_then_disconnect)
    text = f"replacement memory:{new_target}"
    failure = None
    try:
        if mode == "put":
            handler.put(text=text, title="new title", meta={"hook": "new hook"})
        else:
            kwargs: dict[str, Any] = {}
            if mode == "replace":
                kwargs["title"] = "changed title"
            elif mode == "find-replace":
                kwargs["find"] = old_body
            else:
                kwargs.update(find=old_body, where="after")
                text = " " + text
            handler.edit(
                id=mid, mode=mode, text=text, meta={"hook": "changed hook"}, **kwargs
            )
    except Internal as exc:
        failure = exc

    assert len(closed) == 1
    assert closed[0].info.transaction_status == TransactionStatus.UNKNOWN
    assert persisted_state() == before
    assert failure is not None, "closed mention connection returned false success"
    assert "transaction" in str(failure)


# ---------------------------------------------------------------------------
# Attribution gate (docs/backlog/memory-attribution-gate.md)
# ---------------------------------------------------------------------------


def _audit_tags(store: Store, ref_id: int) -> list[str]:
    return [str(t) for t in store.tags_for(ref_id) if str(t).startswith("AUDIT:")]


def test_attribution_gate_tags_and_advises_on_create(
    handler: MemoryHandler, store: Store
) -> None:
    src = id_of(handler.put(text="source note: the film is thick").body)
    bad = handler.put(text=f"the scale is ~10 nm per memory:{src}.")
    bad_id = id_of(bad.body)
    assert (
        f'ungrounded: "10 nm" near memory:{src} — not in the cited text; '
        'drop the attribution or write "(my estimate)"'
    ) in bad.body
    assert _audit_tags(store, bad_id) == ["AUDIT:ungrounded-number"]
    # The header shows the verdict on its own line.
    assert "AUDIT:ungrounded-number:" in handler.get(id=bad_id).body

    grounded_src = id_of(handler.put(text="source: the scale is about 10 nm").body)
    ok = handler.put(text=f"the scale is ~10 nm per memory:{grounded_src}.")
    assert "ungrounded" not in ok.body
    assert _audit_tags(store, id_of(ok.body)) == []

    own = handler.put(text=f"the scale is ~10 nm (my estimate), cf. memory:{src}.")
    assert _audit_tags(store, id_of(own.body)) == []


def test_attribution_gate_clean_rewrite_clears_tag(
    handler: MemoryHandler, store: Store
) -> None:
    src = id_of(handler.put(text="no figures here").body)
    mid = id_of(handler.put(text=f"it is 5 nm per memory:{src}.").body)
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]

    r = handler.edit(
        id=mid,
        mode="find-replace",
        find="5 nm per",
        text="small, see",
    )
    assert "ungrounded" not in r.body
    assert _audit_tags(store, mid) == []

    # Anchored edit that reintroduces the miss re-tags and advises.
    r = handler.edit(id=mid, mode="find-replace", find="small", text="7 nm")
    assert 'ungrounded: "7 nm"' in r.body
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]
    # Whole-body replace with clean prose clears it again.
    r = handler.edit(id=mid, mode="replace", text="no figures at all")
    assert _audit_tags(store, mid) == []


def test_attribution_gate_reject_mode(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = id_of(handler.put(text="nothing numeric").body)
    monkeypatch.setenv("PRECIS_MEMORY_ATTRIBUTION_GATE", "reject")
    with pytest.raises(BadInput, match="ungrounded") as exc:
        handler.put(text=f"it is 5 nm per memory:{src}.")
    assert "my estimate" in (exc.value.next or "")
    # An unresolvable cite is not a grounding failure, even in reject mode.
    handler.put(text="it is 5 nm per memory:99999999.")


def test_agent_can_tag_audit_on_memory(handler: MemoryHandler, store: Store) -> None:
    mid = id_of(handler.put(text="plain note").body)
    handler.tag(id=mid, add=["AUDIT:ungrounded-number"])
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]


def test_attribution_fail_open_keeps_existing_flag(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    src = id_of(handler.put(text="no figures here").body)
    mid = id_of(handler.put(text=f"it is 5 nm per memory:{src}.").body)
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr("precis.handlers.memory.ungrounded_cited_numbers", boom)
    with caplog.at_level(logging.WARNING, logger="precis.handlers.memory"):
        assert handler._attribution_misses("anything 5 nm memory:1") is None
    warned = [r for r in caplog.records if "failed open" in r.getMessage()]
    assert warned, "the fail-open path must log"
    assert warned[0].exc_info is not None, "with the traceback, or it is undebuggable"
    handler.edit(id=mid, mode="replace", text="no figures at all")
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]


def test_attribution_clean_rewrite_keeps_hand_set_flag(
    handler: MemoryHandler, store: Store
) -> None:
    mid = id_of(handler.put(text="plain note").body)
    handler.tag(id=mid, add=["AUDIT:ungrounded-number"])  # set_by agent
    handler.edit(id=mid, mode="replace", text="still plain, no figures")
    assert _audit_tags(store, mid) == ["AUDIT:ungrounded-number"]
