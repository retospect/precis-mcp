"""Quest → paper tagging — scoping the Drive browse surface to one quest.

The hub's "servers-lite" footer (:mod:`precis.quest.gaps`) counts a quest's
``serves``-linked papers but the count used to link to the generic
``/refs/paper`` browse — every paper in the corpus, not this quest's. Drive
already supports ``?tag=<value>`` (an OPEN-tag facet), so stamping every
serving paper with ``quest:<public_id>`` turns that generic link into a
quest-scoped one for free, no new query surface.

Scope is deliberately narrow: **only** papers with a live inbound ``serves``
link to the quest are tagged — exactly the set :func:`precis.quest.gaps
._live_servers` counts, so the hub's "N papers" figure and the Drive filter
it links to always agree. ``add_tag`` is idempotent (``ON CONFLICT DO
UPDATE``), so re-running this over an already-tagged quest is a safe no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import NotFound
from precis.quest.gaps import _live_servers
from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store import Store


def quest_tag_value(quest_id: int, store: Store) -> str:
    """The ``quest:<public_id>`` OPEN-tag value for one quest.

    Mirrors :attr:`precis.store.types.Ref.public_id` (slug for slug kinds,
    ``str(id)`` for numeric kinds), computed by hand rather than read off
    the property so it works against any duck-typed ``Ref``-alike (e.g. the
    web layer's fake test store), not just the real dataclass. Quest is a
    numeric-id kind — no ``ref_identifiers`` slug is ever minted for it (see
    :mod:`precis.quest.catalyst_seed`) — so this is ``quest:<quest_id>`` in
    practice, but resolves through the ref's slug first so a future
    slug-bearing quest kind would pick one up automatically.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    if ref is None:
        raise NotFound(f"quest id={quest_id} not found")
    slug = getattr(ref, "slug", None)
    return f"quest:{slug if slug is not None else ref.id}"


def tag_serving_papers(store: Store, quest_id: int) -> int:
    """Tag every live ``serves``-linked paper of ``quest_id`` with
    ``quest:<public_id>``. Returns the count tagged (idempotent — a paper
    that already carries the tag is untouched by ``add_tag``'s upsert)."""
    tag_value = quest_tag_value(quest_id, store)
    tag = Tag.open(tag_value)
    papers = [s for s in _live_servers(store, quest_id) if s.kind == "paper"]
    for paper in papers:
        store.add_tag(paper.id, tag, set_by="system")
    return len(papers)


__all__ = ["quest_tag_value", "tag_serving_papers"]
