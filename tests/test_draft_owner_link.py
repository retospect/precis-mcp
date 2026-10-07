"""``draft-of`` owner link: removable through the verb, retired owners
skipped by readers, hygiene audit (gr461762)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _todo(hub: Hub, title: str) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title=title).id


def _draft(hub: Hub, name: str, proj: int) -> int:
    ref, _ = hub.live_store.drafts.create_draft(
        name=name, title=name, project_ref_id=proj
    )
    return ref.id


def test_remove_unbinds_owner(draft: DraftHandler, hub: Hub) -> None:
    proj = _todo(hub, "P")
    d = _draft(hub, "own-rm", proj)
    assert hub.live_store.drafts.draft_owner(d) == proj
    draft.link(id="own-rm", target=f"todo:{proj}", rel="draft-of", mode="remove")
    assert hub.live_store.drafts.draft_owner(d) is None
    assert not hub.live_store.links_for(d, direction="out", relation="draft-of")


def test_add_refuses_second_live_owner_then_remove_add(
    draft: DraftHandler, hub: Hub
) -> None:
    p1, p2 = _todo(hub, "P1"), _todo(hub, "P2")
    d = _draft(hub, "own-add", p1)
    with pytest.raises(BadInput) as ei:
        draft.link(id="own-add", target=f"todo:{p2}", rel="draft-of")
    assert "mode='remove'" in str(ei.value.next)
    draft.link(id="own-add", target=f"todo:{p1}", rel="draft-of", mode="remove")
    draft.link(id="own-add", target=f"todo:{p2}", rel="draft-of")
    assert hub.live_store.drafts.draft_owner(d) == p2


def test_add_allowed_when_owner_retired(draft: DraftHandler, hub: Hub) -> None:
    p1, p2 = _todo(hub, "P1"), _todo(hub, "P2")
    d = _draft(hub, "own-ret", p1)
    hub.live_store.retire_ref(p1)
    draft.link(id="own-ret", target=f"todo:{p2}", rel="draft-of")
    assert hub.live_store.drafts.draft_owner(d) == p2


def test_draft_owner_skips_retired(hub: Hub) -> None:
    store = hub.live_store
    p1, p2 = _todo(hub, "P1"), _todo(hub, "P2")
    d = _draft(hub, "own-skip", p1)
    store.add_link(src_ref_id=d, dst_ref_id=p2, relation="draft-of")
    assert store.drafts.draft_owner(d) == p1  # two live: lowest id, deterministic
    store.retire_ref(p1)
    assert store.drafts.draft_owner(d) == p2
    store.retire_ref(p2)
    assert store.drafts.draft_owner(d) is None


def test_owner_audit_counts_dangling_and_multi(hub: Hub) -> None:
    store = hub.live_store
    base = store.drafts.draft_owner_audit()
    p1, p2, p3 = _todo(hub, "A"), _todo(hub, "B"), _todo(hub, "C")
    dangling = _draft(hub, "aud-dangling", p1)
    store.retire_ref(p1)
    doubled = _draft(hub, "aud-doubled", p2)
    store.add_link(src_ref_id=doubled, dst_ref_id=p3, relation="draft-of")
    got = store.drafts.draft_owner_audit()
    assert set(got["dangling"]) - set(base["dangling"]) == {dangling}
    assert set(got["multi_owner"]) - set(base["multi_owner"]) == {doubled}
