"""``SPACE:`` closed axis (docs/backlog/memory-native-authoring.md AC 1 + 2).

Registered code-only in ``_CLOSED_VOCAB`` with values research / repo-dev /
personal; allowed on memory, skill, todo and gripe; ``MemoryHandler`` stamps
``SPACE:research`` by default and a caller's ``SPACE:`` replaces it.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag
from tests.conftest import id_of

_VALUES = ["personal", "repo-dev", "research"]


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _space_tags(store: Store, ref_id: int) -> list[str]:
    return sorted(str(t) for t in store.tags_for(ref_id) if str(t).startswith("SPACE:"))


def test_tag_verb_accepts_each_registered_value_on_memory(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text="a memory").body)
    handler.tag(id=ref_id, add=["SPACE:repo-dev"])
    assert "SPACE:repo-dev" in _space_tags(store, ref_id)


def test_tag_verb_rejects_unknown_value_listing_the_three(
    handler: MemoryHandler,
) -> None:
    ref_id = id_of(handler.put(text="a memory").body)
    with pytest.raises(BadInput, match="invalid SPACE value") as exc:
        handler.tag(id=ref_id, add=["SPACE:lab"])
    assert exc.value.options == _VALUES
    assert all(v in str(exc.value.next) for v in _VALUES)


def test_space_not_allowed_on_paper() -> None:
    with pytest.raises(BadInput, match="axis not allowed on kind 'paper'"):
        Tag.parse_strict("SPACE:repo-dev", kind="paper")


@pytest.mark.parametrize("kind", ["memory", "skill", "todo", "gripe"])
def test_space_allowed_on_listed_kinds(kind: str) -> None:
    for value in _VALUES:
        tag = Tag.parse_strict(f"SPACE:{value}", kind=kind)
        assert (tag.namespace, tag.prefix, tag.value) == ("closed", "SPACE", value)


@pytest.mark.parametrize("kind", ["finding", "markdown"])
def test_space_accepted_on_unlisted_kinds_without_stripping_other_axes(
    kind: str,
) -> None:
    # finding / markdown are deliberately unlisted in _KIND_ALLOWED_AXES
    # (listing finding would strip its other axes) — SPACE passes anyway.
    assert str(Tag.parse_strict("SPACE:research", kind=kind)) == "SPACE:research"
    assert str(Tag.parse_strict("TAPROOT:claim", kind="finding")) == "TAPROOT:claim"


def test_put_without_space_lands_research(handler: MemoryHandler, store: Store) -> None:
    ref_id = id_of(handler.put(text="no space given").body)
    assert _space_tags(store, ref_id) == ["SPACE:research"]


def test_put_with_explicit_space_replaces_the_default(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text="dev note", tags=["SPACE:repo-dev"]).body)
    assert _space_tags(store, ref_id) == ["SPACE:repo-dev"]


def test_put_keeps_open_tags_next_to_the_default(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text="with a tag", tags=["kind:decision"]).body)
    tags = {str(t) for t in store.tags_for(ref_id)}
    assert {"SPACE:research", "kind:decision"} <= tags
