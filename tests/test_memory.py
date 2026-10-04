"""MemoryHandler — through both direct invocation and full dispatch."""

from __future__ import annotations

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
    with pytest.raises(BadInput, match=r"allowed keys: \['hook'\]"):
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
