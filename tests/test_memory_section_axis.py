"""``section:`` is a closed axis on memory, per SPACE (gr688811): the allowed
set is what the space's root hub names in its body, and ``put`` / ``tag``
refuse a value outside it."""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, _created_id, audit_sections
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.store import Store

HUB = "section:index"
ROOT_BODY = (
    "Root of the graph. Hubs are tagged section:index. Each node also carries "
    "one type tag: section:threads (in-flight), section:runbooks, "
    "section:gotchas, section:workflow, section:reference."
)


def _note(handler: MemoryHandler, title: str, body: str, *tags: str) -> int:
    return _created_id(handler.put(text=body, title=title, tags=list(tags)))


def _root(handler: MemoryHandler, store: Store, body: str = ROOT_BODY) -> int:
    """A space root: a parentless hub another hub hangs off."""
    root = _note(handler, "Graph root", body, SPACE_TAG, HUB)
    sub = _note(handler, "Subject hub", "members", SPACE_TAG, HUB)
    store.add_link(src_ref_id=sub, dst_ref_id=root, relation="part-of")
    return root


def _section_tags(store: Store, ref_id: int) -> set[str]:
    return {
        v
        for _ns, v in store.ref_tags_bulk([ref_id]).get(ref_id, [])
        if v.startswith("section:")
    }


def test_put_refuses_a_section_the_root_does_not_register(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    _root(handler, store)
    before = len(store.list_refs(kind="memory", limit=1000))

    with pytest.raises(BadInput) as exc:
        _note(handler, "A ruling", "what was ruled", SPACE_TAG, "section:rulings")

    assert "'rulings'" in str(exc.value)
    assert exc.value.options == [
        "section:gotchas",
        "section:index",
        "section:reference",
        "section:runbooks",
        "section:threads",
        "section:workflow",
    ]
    assert len(store.list_refs(kind="memory", limit=1000)) == before


def test_put_accepts_a_registered_section(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    _root(handler, store)

    node = _note(handler, "How we work", "the rule", SPACE_TAG, "section:workflow")

    assert _section_tags(store, node) == {"section:workflow"}


def test_a_value_added_to_the_root_is_then_accepted(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _root(handler, store)
    with pytest.raises(BadInput):
        _note(handler, "A ruling", "what was ruled", SPACE_TAG, "section:rulings")

    handler.edit(id=root, mode="append", text=" Rulings carry section:rulings.")
    node = _note(handler, "A ruling", "what was ruled", SPACE_TAG, "section:rulings")

    assert _section_tags(store, node) == {"section:rulings"}


def test_tag_refuses_an_unregistered_section_and_can_still_remove_one(
    hub: Hub,
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    legacy = _note(handler, "Legacy", "tagged early", SPACE_TAG, "section:rulings")
    node = _note(handler, "Plain", "no type yet", SPACE_TAG)
    _root(handler, store)

    with pytest.raises(BadInput, match="rulings"):
        handler.tag(id=node, add=["section:rulings"])
    with pytest.raises(BadInput, match="rulings"):
        handler.tag(id=[node, legacy], add=["section:rulings"])
    assert _section_tags(store, node) == set()

    handler.tag(id=node, add=["section:gotchas"])
    handler.tag(id=legacy, remove=["section:rulings"], add=["section:workflow"])

    assert _section_tags(store, node) == {"section:gotchas"}
    assert _section_tags(store, legacy) == {"section:workflow"}


def test_a_space_without_a_registering_root_stays_open(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    _root(handler, store)
    bare = _note(handler, "Other root", "names no type", "SPACE:research", HUB)
    sub = _note(handler, "Other hub", "members", "SPACE:research", HUB)
    store.add_link(src_ref_id=sub, dst_ref_id=bare, relation="part-of")

    node = _note(handler, "Chapter", "prose", "SPACE:research", "section:outlook")
    handler.tag(id=node, add=["section:dft_methodology"])

    assert _section_tags(store, node) == {
        "section:outlook",
        "section:dft_methodology",
    }


def test_edit_checks_stored_sections_and_allows_repair(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    legacy = _note(handler, "Legacy", "old body", SPACE_TAG, "section:rulings")
    valid = _note(handler, "Valid", "original body", SPACE_TAG, "section:workflow")
    root = _root(handler, store)

    with pytest.raises(BadInput, match="rulings"):
        handler.edit(id=legacy, title="New title", dry_run=True)
    with pytest.raises(BadInput, match="batch"):
        handler.edit(id=[valid, legacy], mode="append", text=" New text.")
    assert "New text." not in handler.get(id=valid).body

    handler.edit(id=root, mode="append", text=" Rulings carry section:rulings.")
    handler.edit(id=legacy, mode="append", text=" New text.")
    assert "New text." in handler.get(id=legacy).body


def test_audit_reports_only_legacy_tags_in_closed_spaces(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    legacy = _note(handler, "Legacy", "live", SPACE_TAG, "section:rulings")
    retired = _note(handler, "Retired", "gone", SPACE_TAG, "section:rulings")
    store.retire_ref(retired)
    _note(handler, "Chapter", "prose", "SPACE:research", "section:outlook")
    _root(handler, store)

    assert audit_sections(store).splitlines() == [
        "1 unregistered section tag(s)",
        f"me{legacy} {SPACE_TAG} section:rulings",
    ]
    handler.tag(id=legacy, remove=["section:rulings"], add=["section:workflow"])
    assert audit_sections(store) == "0 unregistered section tag(s)"
