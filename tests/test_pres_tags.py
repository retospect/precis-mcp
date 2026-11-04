"""MCP presentation overviews and tag views expose the same stored tags."""

import pytest

from precis.dispatch import Hub
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.mark.parametrize("view", [None, "tags"])
def test_pres_reads_show_added_tags_and_hide_removed_tags(
    runtime_with_store: PrecisRuntime, store: Store, view: str | None
) -> None:
    ref = store.insert_ref(kind="pres", slug="tag-view-deck", title="Tag view")
    handler = PresentationHandler(hub=Hub(store=store))
    handler.tag(id=ref.id, add=["topic:slides", "starred"])

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "pres", "id": f"pr{ref.id}", "view": view}
    )

    assert not is_error, body
    line = next(line for line in body.splitlines() if line.startswith("tags: "))
    assert set(line.removeprefix("tags: ").split(", ")) == {"topic:slides", "starred"}
    handler.tag(id=ref.id, remove=["starred"])
    updated = handler.get(id=f"pr{ref.id}", view=view).body
    assert "tags: topic:slides" in updated
    assert "starred" not in updated


def test_pres_tags_view_is_declared_and_supports_path_addressing(store: Store) -> None:
    ref = store.insert_ref(kind="pres", slug="tagless-deck", title="No tags")
    handler = PresentationHandler(hub=Hub(store=store))

    assert "tags" in handler.accepted_views()
    assert (
        handler.get(id=f"pr{ref.id}/tags").body == f"# pr{ref.id} — tags\ntags: (none)"
    )
