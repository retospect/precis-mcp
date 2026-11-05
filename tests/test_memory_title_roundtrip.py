"""gr563668: memory title decoration must not become authored body prose."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert, Tag


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _legacy_memory(
    store: Store, title: str, body: str, *, thread: bool = False, echo: bool = True
) -> int:
    """Seed the old chunk format without going through current put metadata."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory",
            slug=None,
            title=title,
            meta={"hook": "Preserve this independent retrieval hint"},
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"{title}\n\n{body}" if echo else body,
                    meta={"chunk_kind": "memory_body"},
                )
            ],
            conn=conn,
        )
        if thread:
            store.add_tag(ref.id, Tag.open("section:threads"), conn=conn)
    return int(ref.id)


def _displayed_body(handler: MemoryHandler, store: Store, ref_id: int) -> str:
    """Read the ordinary get, removing its header and optional tag decoration."""
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    rendered = handler.get(id=ref_id).body
    header = f"# memory {ref_id}: {ref.title}"
    assert rendered.startswith(header + "\n\n"), rendered
    return rendered[len(header) + 2 :].split("\n\ntags: ", 1)[0]


def test_metadata_retitle_preserves_legacy_body_and_chunks(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Old code exposes the hidden prefix on the first metadata-only retitle."""
    handler = MemoryHandler(hub=runtime_with_store.hub)
    body = "Historical context\n\n  Keep τ and spacing.  \n"
    ref_id = _legacy_memory(store, "Legacy title", body)
    chunks = store.chunks.list_chunks_for_ref(ref_id)
    assert _displayed_body(handler, store, ref_id) == body

    for title in ("A new claim", "Historical context", "Another claim"):
        receipt = runtime_with_store.dispatch(
            "edit",
            {
                "kind": "memory",
                "id": ref_id,
                "__extras__": {"title": title},
            },
        )
        assert "[error" not in receipt, receipt
        assert _displayed_body(handler, store, ref_id) == body
        assert [
            chunk
            for chunk in store.chunks.list_chunks_for_ref(ref_id)
            if chunk.chunk_kind == "memory_body"
        ] == chunks
        ref = store.get_ref(kind="memory", id=ref_id)
        assert ref is not None
        assert ref.meta["hook"] == "Preserve this independent retrieval hint"
    with store.tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM ref_events "
            "WHERE ref_id = %s AND event = 'body_replaced'",
            (ref_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "replacement",
    [
        "Historical context changed\n\n  Keep τ and spacing.  \n",
        "New claim\n\n  Keep τ and spacing.  \n",
        "Legacy title\n\n  This submitted lead is now prose.  \n",
    ],
    ids=["historical-lead", "lead-equals-new-title", "lead-equals-old-title"],
)
def test_full_displayed_body_rewrite_with_title_roundtrips_exactly(
    runtime_with_store: PrecisRuntime, store: Store, replacement: str
) -> None:
    """The displayed-body anchor must consume the hidden legacy prefix too."""
    handler = MemoryHandler(hub=runtime_with_store.hub)
    old_body = "Historical context before the rewrite\n\nEvidence remains."
    old_title = "Legacy title"
    ref_id = _legacy_memory(store, old_title, old_body)
    before = store.chunks.list_chunks_for_ref(ref_id)
    find = _displayed_body(handler, store, ref_id)
    assert find == old_body

    receipt = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": ref_id,
            "mode": "find-replace",
            "find": find,
            "text": replacement,
            "__extras__": {"title": "New claim"},
        },
    )
    assert "[error" not in receipt, receipt
    assert _displayed_body(handler, store, ref_id) == replacement
    after = [
        chunk
        for chunk in store.chunks.list_chunks_for_ref(ref_id)
        if chunk.chunk_kind == "memory_body"
    ]
    assert [chunk.text for chunk in after] == [replacement]
    assert {chunk.id for chunk in before}.isdisjoint(chunk.id for chunk in after)
    with store.tx() as conn:
        event = conn.execute(
            "SELECT payload FROM ref_events "
            "WHERE ref_id = %s AND event = 'body_replaced' "
            "ORDER BY event_id DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
    assert event is not None
    assert event[0]["old_text"] == f"{old_title}\n\n{old_body}"
    assert event[0]["new_text"] == replacement


def test_explicit_rewrite_keeps_a_submitted_lead_equal_to_title(
    handler: MemoryHandler, store: Store
) -> None:
    """Old display dedup strips this deliberately submitted first paragraph."""
    ref_id = _legacy_memory(store, "Legacy title", "Previous prose.")
    replacement = "New claim\n\n  Exact α whitespace.  \n\n"
    handler.edit(id=ref_id, mode="replace", text=replacement, title="New claim")
    assert _displayed_body(handler, store, ref_id) == replacement
    section = handler.get(id=f"{ref_id}~body").body
    prefix = f"# memory {ref_id}~body\n\n"
    assert section.startswith(prefix)
    assert section[len(prefix) :].split("\nNext:\n", 1)[0] == replacement
    handler.edit(id=ref_id, title="Later header")
    assert _displayed_body(handler, store, ref_id) == replacement


@pytest.mark.parametrize("batch", [False, True], ids=["single", "ops"])
@pytest.mark.parametrize("preview", [False, True], ids=["write", "dry-run"])
def test_full_displayed_body_anchor_uses_the_same_projection_in_ops_and_preview(
    handler: MemoryHandler, store: Store, batch: bool, preview: bool
) -> None:
    """Old writes retain the prefix; old full previews print it as context."""
    body = "Previous prose."
    ref_id = _legacy_memory(store, "Legacy title", body)
    replacement = "New historical context\n\nRevised evidence."
    chunks_before = store.chunks.list_chunks_for_ref(ref_id)
    ref_before = store.get_ref(kind="memory", id=ref_id)
    dry_run: bool | str = "full" if preview else False
    if batch:
        receipt = handler.edit(
            id=ref_id,
            ops=[{"find": body, "text": replacement}],
            dry_run=dry_run,
        )
    else:
        receipt = handler.edit(
            id=ref_id,
            mode="find-replace",
            find=body,
            text=replacement,
            dry_run=dry_run,
        )
    if preview:
        assert "Legacy title" not in receipt.body
        assert "New historical context" in receipt.body
        assert store.get_ref(kind="memory", id=ref_id) == ref_before
        assert store.chunks.list_chunks_for_ref(ref_id) == chunks_before
    else:
        assert _displayed_body(handler, store, ref_id) == replacement
        assert [
            chunk.text
            for chunk in store.chunks.list_chunks_for_ref(ref_id)
            if chunk.chunk_kind == "memory_body"
        ] == [replacement]


def test_previously_retitled_legacy_row_keeps_old_prefix_without_repair(
    handler: MemoryHandler, store: Store
) -> None:
    """Unstamped pre-fix retitles retain the old prefix; reads never repair it."""
    body = "Historical context\n\nOriginal evidence.  \n"
    ref_id = _legacy_memory(store, "Legacy title", body)
    store.chunks.set_ref_title(ref_id, "Already changed", source="agent")
    store.chunks.set_ref_title(ref_id, "Changed again", source="agent")
    ref_before = store.get_ref(kind="memory", id=ref_id)
    chunks_before = store.chunks.list_chunks_for_ref(ref_id)
    with store.tx() as conn:
        events_before = conn.execute(
            "SELECT event_id, event, payload FROM ref_events "
            "WHERE ref_id = %s ORDER BY event_id",
            (ref_id,),
        ).fetchall()

    assert _displayed_body(handler, store, ref_id) == f"Legacy title\n\n{body}"
    assert store.get_ref(kind="memory", id=ref_id) == ref_before
    assert store.chunks.list_chunks_for_ref(ref_id) == chunks_before
    with store.tx() as conn:
        assert (
            conn.execute(
                "SELECT event_id, event, payload FROM ref_events "
                "WHERE ref_id = %s ORDER BY event_id",
                (ref_id,),
            ).fetchall()
            == events_before
        )


@pytest.mark.parametrize(
    "tail_edit", [False, True], ids=["retitle", "retitle-tail-edit"]
)
def test_stamped_retitle_matching_body_lead_does_not_invent_title_echo(
    handler: MemoryHandler, store: Store, tail_edit: bool
) -> None:
    """A coincident new title must not hide prose that was always visible."""
    lead = "Historical context"
    body = f"{lead}\n\n  Original evidence with α.  \n"
    ref_id = _legacy_memory(store, "Independent original title", body, echo=False)
    assert _displayed_body(handler, store, ref_id) == body
    chunks_before = store.chunks.list_chunks_for_ref(ref_id)

    handler.edit(id=ref_id, title=lead)
    if tail_edit:
        handler.edit(
            id=ref_id,
            mode="find-replace",
            find="Original evidence",
            text="Updated evidence",
        )
        body = body.replace("Original evidence", "Updated evidence")

    assert _displayed_body(handler, store, ref_id) == body
    if not tail_edit:
        assert [
            chunk
            for chunk in store.chunks.list_chunks_for_ref(ref_id)
            if chunk.chunk_kind == "memory_body"
        ] == chunks_before


def test_body_lead_edit_keeps_its_derived_echo_hidden_after_retitle(
    handler: MemoryHandler, store: Store
) -> None:
    """A current retitle stamps the echo from the derived title it locks."""
    ref_id = _legacy_memory(store, "Old", "tail")
    handler.edit(id=ref_id, mode="find-replace", find="Old", text="New")
    assert _displayed_body(handler, store, ref_id) == "tail"

    handler.edit(id=ref_id, title="Later header")

    assert _displayed_body(handler, store, ref_id) == "tail"


def test_exact_rewrite_keeps_authored_lead_when_a_later_anchor_edits_it(
    handler: MemoryHandler, store: Store
) -> None:
    """Title rederivation must not turn exact authored prose into decoration."""
    ref_id = _legacy_memory(store, "Old", "tail")
    body = "Written lead\n\ntail"
    handler.edit(id=ref_id, mode="replace", text=body, title="Written lead")
    assert _displayed_body(handler, store, ref_id) == body

    handler.edit(
        id=ref_id,
        mode="find-replace",
        find="Written lead",
        text="Changed lead",
    )

    assert _displayed_body(handler, store, ref_id) == "Changed lead\n\ntail"


@pytest.mark.parametrize("batch", [False, True], ids=["single", "ops"])
@pytest.mark.parametrize(
    ("match", "nth", "before", "after", "expected"),
    [
        ("nth", 2, "", "", "A\n\nB"),
        ("first", None, "", "", "B\n\nA"),
        ("all", None, "", "", "B\n\nB"),
        ("unique", None, "A\n\n", "", "A\n\nB"),
        ("unique", None, "", "\n\nA", "B\n\nA"),
    ],
)
def test_occurrence_selection_uses_raw_body(
    handler: MemoryHandler,
    store: Store,
    batch: bool,
    match: str,
    nth: int | None,
    before: str,
    after: str,
    expected: str,
) -> None:
    """An occurrence selector cannot replace the whole visible-body projection."""
    ref_id = _legacy_memory(store, "A", "A")
    if batch:
        handler.edit(
            id=ref_id,
            ops=[
                {
                    "find": "A",
                    "text": "B",
                    "match": match,
                    "nth": nth,
                    "before": before,
                    "after": after,
                }
            ],
        )
    else:
        handler.edit(
            id=ref_id,
            mode="find-replace",
            find="A",
            text="B",
            match=match,
            nth=nth,
            before=before,
            after=after,
        )
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    assert handler._body_text(ref) == expected
    assert "_memory_body_display" not in ref.meta


def test_create_and_ordinary_body_edits_do_not_stamp(
    handler: MemoryHandler, store: Store
) -> None:
    _, ref_id = handler._create_with_id(
        text="Lead\n\nEvidence and tail", tags=None, link=None
    )
    for action in (None, "find-replace", "append"):
        if action == "find-replace":
            handler.edit(id=ref_id, mode=action, find="Evidence", text="Updated")
        elif action == "append":
            handler.edit(id=ref_id, mode=action, text="More evidence")
        ref = store.get_ref(kind="memory", id=ref_id)
        assert ref is not None
        assert "_memory_body_display" not in ref.meta
        assert ref.title == "Lead"


@pytest.mark.parametrize("mode", ["find-replace", "append"])
def test_ordinary_edit_carries_surviving_retitle_stamp(
    handler: MemoryHandler, store: Store, mode: str
) -> None:
    ref_id = _legacy_memory(store, "Old", "Evidence and tail")
    handler.edit(id=ref_id, title="New header")
    if mode == "find-replace":
        handler.edit(id=ref_id, mode=mode, find="Evidence", text="Updated")
        expected = "Updated and tail"
    else:
        handler.edit(id=ref_id, mode=mode, text="More evidence")
        expected = "Evidence and tail\n\nMore evidence"
    assert _displayed_body(handler, store, ref_id) == expected
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    assert ref.meta["_memory_body_display"] == handler._body_display_policy(
        f"Old\n\n{expected}", "Old"
    )


def test_display_stamp_is_body_bound_and_needs_no_transaction(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_tx() -> None:
        pytest.fail("display decisions must not open a transaction")

    monkeypatch.setattr(store, "tx", forbidden_tx)
    body = "Old\n\nEvidence"
    meta = {"_memory_body_display": handler._body_display_policy(body, "Old")}
    assert handler._body_echo_title(body, "New", meta) == "Old"
    assert handler._body_echo_title("External rewrite", "New", meta) == "New"
    assert handler._body_echo_title(body, "New", {}) == "New"


def test_thread_rewrite_receipt_does_not_suggest_replacing_stale_title(
    handler: MemoryHandler, store: Store
) -> None:
    """Old code mistakes the retained hidden title for an unchanged thread lead."""
    old_body = "Historical context before the rewrite\n\nOriginal evidence."
    ref_id = _legacy_memory(store, "Legacy campaign title", old_body, thread=True)
    replacement = "Historical context after the rewrite\n\nRevised evidence."
    receipt = handler.edit(
        id=ref_id,
        mode="find-replace",
        find=old_body,
        text=replacement,
        title="The campaign preserves its evidence",
    )
    assert "lead line still reads" not in receipt.body
    assert "find='Legacy campaign title'" not in receipt.body
    assert _displayed_body(handler, store, ref_id) == replacement


@pytest.mark.parametrize("find", ["— referenced entities —", "STATUS:open"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "ops"])
def test_generated_footer_anchor_refuses_with_body_only_next_step(
    handler: MemoryHandler, store: Store, find: str, batch: bool
) -> None:
    """Old refusal is generic; callers need a body-only read to recover."""
    target = store.insert_ref(kind="todo", slug=None, title="Evidence follow-up")
    store.add_tag(target.id, Tag.closed("STATUS", "open"))
    authored = f"Historical context refers to td{target.id}."
    ref_id = _legacy_memory(store, "Legacy title", authored)
    assert find in handler.get(id=ref_id).body
    assert find not in authored
    chunks_before = store.chunks.list_chunks_for_ref(ref_id)
    ref_before = store.get_ref(kind="memory", id=ref_id)

    with pytest.raises(BadInput) as caught:
        if batch:
            handler.edit(
                id=ref_id,
                ops=[{"find": find, "text": "This must not be written"}],
                meta={"hook": "This must not be written either"},
            )
        else:
            handler.edit(
                id=ref_id,
                mode="find-replace",
                find=find,
                text="This must not be written",
                title="This must not be written either",
            )
    assert f"get(kind='memory', id='me{ref_id}~body')" in str(caught.value.next)
    assert store.chunks.list_chunks_for_ref(ref_id) == chunks_before
    assert store.get_ref(kind="memory", id=ref_id) == ref_before
