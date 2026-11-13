"""gr626896 — a link ``meta.reason`` that restates an endpoint's displayed
text verbatim is refused at write time.

A link explanation is incremental information: what the edge adds beyond
what the row already shows. ``reason='.'`` (the reviewed-obvious marker,
also spelt ``'reviewed-obvious'``) is the explicit "nothing to add" and is
stored as a period. The check is conservative — whitespace-collapsed
equality against each endpoint's title, ``meta.hook`` and part brief —
never a bag-of-words gate, so shared vocabulary in a real bridge passes
and changed negation/numbers are never normalised away.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.runtime import PrecisRuntime


def _mem(store, title: str, *, hook: str | None = None) -> int:
    return store.insert_ref(
        kind="memory", slug=None, title=title, meta={"hook": hook} if hook else None
    ).id


def test_reason_copying_target_title_is_refused(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "Doppler shift for light at relativistic speeds")
    with pytest.raises(BadInput, match="restates the target endpoint's title"):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            relation="explains",
            meta={"reason": "Doppler shift for light at relativistic speeds"},
        )
    assert store.links_for(src, direction="out") == []


def test_reason_copying_source_title_is_refused(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "thermal drift dominates below 4 K")
    dst = _mem(store, "a related note")
    with pytest.raises(BadInput, match="restates the source endpoint's title"):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            meta={"reason": "thermal drift dominates below 4 K"},
        )


def test_reason_copying_endpoint_hook_is_refused(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(
        store,
        "a long memory title",
        hook="the one-line index summary shown for this node",
    )
    with pytest.raises(BadInput, match="restates the target endpoint's summary"):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            meta={"reason": "the one-line index summary shown for this node"},
        )


def test_whitespace_differences_do_not_rescue_a_copy(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "two word title")
    with pytest.raises(BadInput):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            meta={"reason": "  two\n  word   title "},
        )


def test_same_vocabulary_with_a_bridge_passes(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "Doppler shift for light at relativistic speeds")
    link = store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        meta={
            "reason": "the relativistic Doppler shift here is why the "
            "emission sits below the lab-frame line"
        },
    )
    assert isinstance(link.id, int)


def test_changed_negation_and_numbers_are_not_normalised_away(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    neg = _mem(store, "substrate does not affect the resonance")
    num = _mem(store, "resonance observed at 300 K")
    # Flipped negation and changed number both differ verbatim — accepted.
    store.add_link(
        src_ref_id=src,
        dst_ref_id=neg,
        meta={"reason": "substrate does affect the resonance"},
    )
    store.add_link(
        src_ref_id=src,
        dst_ref_id=num,
        meta={"reason": "resonance observed at 400 K"},
    )


def test_reviewed_obvious_is_accepted_and_stored_as_period(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "Doppler shift for light at relativistic speeds")
    for reason in (".", "reviewed-obvious"):
        link = store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            relation="cites",
            meta={"reason": reason},
        )
        assert link.meta["reason"] == "."
        store.remove_link(src_ref_id=src, dst_ref_id=dst, relation="cites")


def test_link_without_reason_still_writes(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a neighbour")
    link = store.add_link(src_ref_id=src, dst_ref_id=dst)
    assert "reason" not in (link.meta or {})


def test_rejects_reason_still_gated_but_copy_now_refused(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "selection record")
    part = store.insert_ref(kind="part", slug="C25804", title="C25804").id
    with pytest.raises(BadInput, match="non-empty meta.reason"):
        store.add_link(src_ref_id=src, dst_ref_id=part, relation="rejects")
    with pytest.raises(BadInput, match="restates"):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=part,
            relation="rejects",
            meta={"reason": "C25804"},
        )
    link = store.add_link(
        src_ref_id=src,
        dst_ref_id=part,
        relation="rejects",
        meta={"reason": "kept the 0402 twin; this one is EOL per the datasheet"},
    )
    assert link.meta["reason"].startswith("kept the 0402")


# ---------------------------------------------------------------------------
# The reason= kwarg spelling — merge_link_reason folds it onto meta.reason
# on the kinds whose link() declares meta= (memory/finding/paper/taxon/todo,
# gr657045; gripe, gr662070).
# ---------------------------------------------------------------------------


def test_reason_kwarg_folds_onto_meta(hub: Hub) -> None:
    """``link(..., reason=…)`` — the spelling the restatement refusal's
    ``next=`` suggests — lands as ``meta.reason`` on the edge."""
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    MemoryHandler(hub=hub).link(
        id=src, target=f"memory:{dst}", rel="explains", reason="."
    )
    links = store.links_for(src, direction="out")
    assert len(links) == 1
    assert links[0].meta["reason"] == "."


def test_reason_kwarg_canonicalises_reviewed_obvious(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    MemoryHandler(hub=hub).link(
        id=src, target=f"memory:{dst}", reason="reviewed-obvious"
    )
    links = store.links_for(src, direction="out")
    assert links[0].meta["reason"] == "."


def test_reason_kwarg_and_meta_reason_agree_or_refuse(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    handler = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="disagree"):
        handler.link(
            id=src,
            target=f"memory:{dst}",
            reason="the edge adds X",
            meta={"reason": "the edge adds Y"},
        )
    # Same value through both spellings is not an ambiguity.
    handler.link(
        id=src,
        target=f"memory:{dst}",
        reason=".",
        meta={"reason": "."},
    )
    assert store.links_for(src, direction="out")[0].meta["reason"] == "."


def test_reason_kwarg_is_add_only(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    with pytest.raises(BadInput, match="only applies when adding"):
        MemoryHandler(hub=hub).link(
            id=src, target=f"memory:{dst}", mode="remove", reason="."
        )


def test_merge_link_reason_contract() -> None:
    """The shared fold: non-string/blank reasons and a remove-mode call
    refuse; agreement with ``meta['reason']`` is not an ambiguity."""
    from precis.utils.link_tag_ops import merge_link_reason

    assert merge_link_reason(None, None, mode="add") is None
    assert merge_link_reason(None, ".", mode="add") == {"reason": "."}
    assert merge_link_reason({"a": 1}, ".", mode="add") == {"a": 1, "reason": "."}
    # Kinds whose link() doesn't declare reason= never reach here — the
    # extras gate refuses first.
    with pytest.raises(BadInput, match="only applies when adding"):
        merge_link_reason(None, ".", mode="remove")
    with pytest.raises(BadInput, match="non-empty string"):
        merge_link_reason(None, "   ", mode="add")
    with pytest.raises(BadInput, match="non-empty string"):
        merge_link_reason(None, 3, mode="add")
    with pytest.raises(BadInput, match="disagree"):
        merge_link_reason({"reason": "x"}, "y", mode="add")
    assert merge_link_reason({"reason": "."}, ".", mode="add") == {"reason": "."}


# ---------------------------------------------------------------------------
# Render coverage — meta.reason displays where the edge lists.
# ---------------------------------------------------------------------------


def test_links_section_renders_reason(hub: Hub) -> None:
    from precis.handlers._links_render import render_links_section

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        relation="explains",
        meta={"reason": "the mechanism behind the note's claim"},
    )
    ref = store.get_ref(kind="memory", id=src)
    assert ref is not None
    assert "the mechanism behind the note's claim" in render_links_section(store, ref)


def test_links_section_renders_reviewed_obvious_as_period(hub: Hub) -> None:
    from precis.handlers._links_render import render_links_section

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.add_link(
        src_ref_id=src, dst_ref_id=dst, relation="cites", meta={"reason": "."}
    )
    ref = store.get_ref(kind="memory", id=src)
    assert ref is not None
    assert " — ." in render_links_section(store, ref)


def test_links_view_renders_reason(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        relation="cites",
        meta={"reason": "the note's whole argument"},
    )
    body = MemoryHandler(hub=hub).get(id=src, view="links").body
    assert "(cites) — the note's whole argument" in body


def test_eye_ring_renders_reason(hub: Hub) -> None:
    from precis.utils.eye_render import render_eye

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        relation="explains",
        meta={"reason": "the mechanism behind the note's claim"},
    )
    out = render_eye(store, f"me{src}", "fisheye+1hop")
    assert "the mechanism behind the note's claim" in out


def test_eye_ring_renders_reviewed_obvious_as_period(hub: Hub) -> None:
    from precis.utils.eye_render import render_eye

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.add_link(
        src_ref_id=src, dst_ref_id=dst, relation="cites", meta={"reason": "."}
    )
    out = render_eye(store, f"me{src}", "fisheye+1hop")
    assert " — ." in out


def test_eye_ring_reason_survives_edge_collapse(hub: Hub) -> None:
    """Chunk-grounded parallel edges to one far ref fold into one row;
    each distinct reason still renders (gr626896 + gr524665's fold)."""
    from precis.store.types import ChunkInsert
    from precis.utils.eye_render import render_eye

    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "a related note")
    store.chunks.insert_chunks(
        dst,
        [
            ChunkInsert(ord=0, text="first passage", meta={}),
            ChunkInsert(ord=1, text="second passage", meta={}),
        ],
    )
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        dst_pos=0,
        relation="explains",
        meta={"reason": "first why"},
    )
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        dst_pos=1,
        relation="explains",
        meta={"reason": "second why"},
    )
    out = render_eye(store, f"me{src}", "fisheye+1hop")
    assert "first why" in out
    assert "second why" in out


# ---------------------------------------------------------------------------
# gr657045 — link(kind='todo') takes reason= like its numeric-ref siblings;
# the verb-level link signature always advertised it.
# ---------------------------------------------------------------------------


def _todo(store, title: str = "a todo") -> int:
    return store.insert_ref(kind="todo", slug=None, title=title, meta={}).id


def test_todo_reason_kwarg_folds_onto_meta(hub: Hub) -> None:
    """The call the gripe ran: a todo's blocked-by edge records its why."""
    from precis.handlers.todo import TodoHandler
    from precis.store.types import Tag

    store = hub.live_store
    todo = _todo(store, "waits on the gripe fix")
    with store.tx() as conn:
        gripe = int(
            store.insert_ref(
                kind="gripe", slug=None, title="the blocker", meta={}, conn=conn
            ).id
        )
        store.add_tag(gripe, Tag.closed("STATUS", "open"), set_by="agent", conn=conn)
    TodoHandler(hub=hub).link(
        id=todo,
        target=f"gripe:{gripe}",
        rel="blocked-by",
        reason="the gripe names the fix this waits on",
    )
    links = store.links_for(todo, direction="out")
    assert len(links) == 1
    assert links[0].meta["reason"] == "the gripe names the fix this waits on"


def test_todo_meta_kwarg_lands_on_the_edge(hub: Hub) -> None:
    from precis.handlers.todo import TodoHandler

    store = hub.live_store
    todo = _todo(store)
    other = _todo(store, "a neighbour")
    TodoHandler(hub=hub).link(
        id=todo, target=f"todo:{other}", rel="related-to", meta={"why": "siblings"}
    )
    assert store.links_for(todo, direction="out")[0].meta["why"] == "siblings"


def test_todo_reason_kwarg_is_add_only(hub: Hub) -> None:
    from precis.handlers.todo import TodoHandler

    store = hub.live_store
    todo = _todo(store)
    other = _todo(store, "a neighbour")
    with pytest.raises(BadInput, match="only applies when adding"):
        TodoHandler(hub=hub).link(
            id=todo, target=f"todo:{other}", mode="remove", reason="."
        )


def test_todo_reason_refused_on_virtual_parent_edge(hub: Hub) -> None:
    """``rel='parent'`` re-points ``refs.parent_id`` — there is no links
    row for edge meta to live on, so meta=/reason= refuse rather than
    silently drop."""
    from precis.handlers.todo import TodoHandler

    store = hub.live_store
    child = _todo(store, "child")
    parent = _todo(store, "parent")
    handler = TodoHandler(hub=hub)
    with pytest.raises(BadInput, match="virtual"):
        handler.link(id=child, target=f"todo:{parent}", rel="parent", reason=".")
    with pytest.raises(BadInput, match="virtual"):
        handler.link(id=child, target=f"todo:{parent}", rel="parent", meta={"a": 1})
    # …and the move itself still works unadorned.
    handler.link(id=child, target=f"todo:{parent}", rel="parent")
    ref = store.get_ref(kind="todo", id=child)
    assert ref is not None and ref.parent_id == parent


def test_todo_reason_refused_on_fan_in(hub: Hub) -> None:
    """``id=[...]`` fans one edge shape across ids; edge meta is per-edge."""
    from precis.handlers.todo import TodoHandler

    store = hub.live_store
    a = _todo(store, "one")
    b = _todo(store, "two")
    tgt = _todo(store, "shared target")
    with pytest.raises(BadInput, match="single todo id"):
        TodoHandler(hub=hub).link(
            id=[a, b], target=f"todo:{tgt}", rel="related-to", reason="."
        )


def test_todo_link_reason_through_dispatch(
    hub: Hub, runtime_with_store: PrecisRuntime
) -> None:
    """The reported call end to end: the strictness gate passes
    ``reason=`` through to the handler instead of refusing it."""
    store = hub.live_store
    todo = _todo(store, "waits")
    other = _todo(store, "the blocker")
    out = runtime_with_store.dispatch(
        "link",
        {
            "kind": "todo",
            "id": todo,
            "target": f"todo:{other}",
            "rel": "blocked-by",
            "reason": "the blocker names what this waits on",
        },
    )
    assert "linked" in out
    links = store.links_for(todo, direction="out")
    assert links[0].meta["reason"] == "the blocker names what this waits on"


# ---------------------------------------------------------------------------
# gr662070 — link(kind='gripe') takes reason= like its numeric-ref siblings;
# the verb-level link signature always advertised it.
# ---------------------------------------------------------------------------


def _gripe(store, title: str = "a gripe") -> int:
    from precis.store.types import Tag

    with store.tx() as conn:
        rid = int(
            store.insert_ref(
                kind="gripe", slug=None, title=title, meta={}, conn=conn
            ).id
        )
        store.add_tag(rid, Tag.closed("STATUS", "open"), set_by="agent", conn=conn)
    return rid


def test_gripe_reason_kwarg_folds_onto_meta(hub: Hub) -> None:
    """The call the gripe ran: a gripe's related-to edge records its why."""
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store, "the gripe doing the linking")
    dst = _gripe(store, "the gripe it depends on")
    GripeHandler(hub=hub).link(
        id=src,
        target=f"gripe:{dst}",
        rel="related-to",
        reason="this gripe depends on that one's fix",
    )
    links = store.links_for(src, direction="out")
    assert len(links) == 1
    assert links[0].meta["reason"] == "this gripe depends on that one's fix"


def test_gripe_meta_kwarg_lands_on_the_edge(hub: Hub) -> None:
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store)
    dst = _mem(store, "the spec it implements")
    GripeHandler(hub=hub).link(
        id=src, target=f"memory:{dst}", rel="related-to", meta={"why": "spec edge"}
    )
    assert store.links_for(src, direction="out")[0].meta["why"] == "spec edge"


def test_gripe_reason_kwarg_is_add_only(hub: Hub) -> None:
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store)
    dst = _gripe(store, "a neighbour")
    with pytest.raises(BadInput, match="only applies when adding"):
        GripeHandler(hub=hub).link(
            id=src, target=f"gripe:{dst}", mode="remove", reason="."
        )


def test_gripe_reason_kwarg_disagrees_with_meta_reason(hub: Hub) -> None:
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store)
    dst = _gripe(store, "a neighbour")
    with pytest.raises(BadInput, match="disagree"):
        GripeHandler(hub=hub).link(
            id=src,
            target=f"gripe:{dst}",
            reason="the edge adds X",
            meta={"reason": "the edge adds Y"},
        )


def test_gripe_reason_copying_target_title_is_refused(hub: Hub) -> None:
    """The gr626896 title-copy refusal applies to gripe edges too."""
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store, "the linking gripe")
    dst = _mem(store, "Doppler shift for light at relativistic speeds")
    with pytest.raises(BadInput, match="restates the target endpoint's title"):
        GripeHandler(hub=hub).link(
            id=src,
            target=f"memory:{dst}",
            reason="Doppler shift for light at relativistic speeds",
        )
    assert store.links_for(src, direction="out") == []


def test_gripe_reason_refused_on_fan_in(hub: Hub) -> None:
    """``id=[...]`` fans one edge shape across ids; edge meta is per-edge."""
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    a = _gripe(store, "one")
    b = _gripe(store, "two")
    tgt = _gripe(store, "shared target")
    with pytest.raises(BadInput, match="single id"):
        GripeHandler(hub=hub).link(
            id=[a, b], target=f"gripe:{tgt}", rel="related-to", reason="."
        )


def test_base_link_refuses_reason_on_kinds_without_link_meta(hub: Hub) -> None:
    """Only ``link_meta`` kinds take edge meta on the base ``link`` path;
    the rest refuse it rather than storing an undocumented edge why."""
    from precis.handlers.anki import AnkiHandler

    with pytest.raises(BadInput, match="does not accept meta=/reason="):
        AnkiHandler(hub=hub).link(id=1, target="gripe:2", reason="why")


def test_gripe_link_reason_through_dispatch(
    hub: Hub, runtime_with_store: PrecisRuntime
) -> None:
    """The reported call end to end: the strictness gate passes
    ``reason=`` through to the handler instead of refusing it."""
    store = hub.live_store
    src = _gripe(store, "the linking gripe")
    dst = _gripe(store, "the spec dependency")
    out = runtime_with_store.dispatch(
        "link",
        {
            "kind": "gripe",
            "id": src,
            "target": f"gripe:{dst}",
            "rel": "related-to",
            "reason": "this gripe implements that spec section",
        },
    )
    assert "linked" in out
    links = store.links_for(src, direction="out")
    assert links[0].meta["reason"] == "this gripe implements that spec section"


def test_gripe_links_view_renders_reason(hub: Hub) -> None:
    """The stored edge why renders in the gripe's links view (acceptance)."""
    from precis.handlers.gripe import GripeHandler

    store = hub.live_store
    src = _gripe(store, "the linking gripe")
    dst = _gripe(store, "the spec dependency")
    store.add_link(
        src_ref_id=src,
        dst_ref_id=dst,
        relation="related-to",
        meta={"reason": "this gripe implements that spec section"},
    )
    body = GripeHandler(hub=hub).get(id=src, view="links").body
    assert "this gripe implements that spec section" in body
