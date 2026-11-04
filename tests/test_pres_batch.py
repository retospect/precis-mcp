"""Presentation tag/edit lists use atomic writes and per-ref receipts."""

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._numeric_ref import BATCH_ID_CAP
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.mark.parametrize("verb", ["tag", "edit"])
def test_batch_mutates_each_deck_with_per_id_results(
    runtime_with_store: PrecisRuntime, store: Store, verb: str
) -> None:
    first = store.insert_ref(kind="pres", slug="batch-first", title="First")
    second = store.insert_ref(kind="pres", slug="batch-second", title="Second")
    third = store.insert_ref(kind="pres", slug="batch-third", title="Third")
    args: dict[str, Any] = {
        "kind": "pres",
        "id": [first.id, f"pr{second.id}", third.slug],
    }
    if verb == "tag":
        args.update(add=["topic:batch"])
    else:
        args.update(title="Updated", meta={"venue": "Workshop"})

    body, is_error = runtime_with_store.dispatch_with_status(verb, args)

    assert not is_error, body
    assert (
        f"{'tagged' if verb == 'tag' else 'edited'} 3 pres in one transaction" in body
    )
    for original in (first, second, third):
        assert f"pr{original.id}:" in body or f"{original.slug}:" in body
        ref = store.get_ref(kind="pres", id=original.id)
        assert ref is not None
        if verb == "tag":
            assert "topic:batch" in {str(t) for t in store.tags_for(ref.id)}
        else:
            assert ref.title == "Updated"
            assert ref.meta["venue"] == "Workshop"


@pytest.mark.parametrize("verb", ["tag", "edit"])
def test_batch_missing_deck_rolls_back_every_write(
    runtime_with_store: PrecisRuntime, store: Store, verb: str
) -> None:
    ref = store.insert_ref(kind="pres", slug="batch-kept", title="Kept")
    args: dict[str, Any] = {"kind": "pres", "id": [ref.id, "absent-deck"]}
    args.update({"add": ["topic:batch"]} if verb == "tag" else {"title": "Changed"})

    body, is_error = runtime_with_store.dispatch_with_status(verb, args)

    assert is_error
    assert "rolled back, nothing applied" in body
    kept = store.get_ref(kind="pres", id=ref.id)
    assert kept is not None and kept.title == "Kept"
    assert "topic:batch" not in {str(t) for t in store.tags_for(ref.id)}


@pytest.mark.parametrize("verb", ["tag", "edit"])
def test_batch_numeric_id_does_not_target_competing_slug(
    runtime_with_store: PrecisRuntime, store: Store, verb: str
) -> None:
    target = store.insert_ref(kind="pres", slug="batch-target", title="Target")
    other = store.insert_ref(kind="pres", slug=str(target.id), title="Other")
    args: dict[str, Any] = {"kind": "pres", "id": [target.id]}
    args.update({"add": ["topic:batch"]} if verb == "tag" else {"title": "Changed"})

    body, is_error = runtime_with_store.dispatch_with_status(verb, args)

    assert not is_error, body
    kept = store.get_ref(kind="pres", id=other.id)
    changed = store.get_ref(kind="pres", id=target.id)
    assert kept is not None and kept.title == "Other"
    assert changed is not None
    assert "topic:batch" not in {str(t) for t in store.tags_for(other.id)}
    if verb == "tag":
        assert "topic:batch" in {str(t) for t in store.tags_for(target.id)}
    else:
        assert changed.title == "Changed"


def test_tag_batch_deduplicates_aliases_and_removes_tags(store: Store) -> None:
    ref = store.insert_ref(kind="pres", slug="batch-alias", title="Aliases")
    handler = PresentationHandler(hub=Hub(store=store))
    handler.tag(id=ref.id, add=["topic:old"])
    response = handler.tag(
        id=[ref.id, f"pr{ref.id}", ref.slug],
        add=["topic:new"],
        remove=["topic:old"],
    )
    assert response.body.startswith("tagged 1 pres in one transaction")
    assert len(response.body.splitlines()) == 2
    assert {str(t) for t in store.tags_for(ref.id)} == {"topic:new"}


@pytest.mark.parametrize(
    "ids", [[], [None], [True], [["nested"]], ["x"] * (BATCH_ID_CAP + 1)]
)
def test_tag_batch_rejects_invalid_lists(store: Store, ids: list[Any]) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    with pytest.raises(BadInput):
        handler.tag(id=ids, add=["topic:batch"])
