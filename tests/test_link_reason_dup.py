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
# on the kinds whose link() declares meta= (memory/finding/paper/taxon).
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
