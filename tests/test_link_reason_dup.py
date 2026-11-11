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
