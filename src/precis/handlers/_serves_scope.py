"""``serves=<quest>`` cohort resolver — shared by the paper search path
and the ``sort=``/``since=``/``until=`` source-search primitive.

``args={'serves': 'quest:<id>'}`` (gr503059 on ``search(kind='paper')``;
gr608733 — the same spelling inside a dated search died on the source
search's unapplied-args gate) restricts hits to the refs serving that
quest: the src end of the quest's inbound ``serves`` edges, the same
listing ``get(kind='quest', view='served')`` renders. The cohort is
kind-agnostic — a todo, se design or finding serves a quest as readily
as a paper; the caller's ``kind=`` scopes the hits, not the set.

Accepts ``quest:<id>``, a ``qu<id>`` handle or a bare numeric id. A named
target that resolves to no live quest raises rather than degrading to an
unfiltered search — a filter the caller named must never silently widen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store.store import Store


def serves_quest_id(store: Store, serves: Any) -> int:
    """The ref_id of the quest ``serves=`` names — the parse +
    existence check :func:`serving_ref_ids` shares with the SQL-side
    ``serves`` filters."""
    token = str(serves).strip()
    if token.startswith("quest:"):
        token = token[len("quest:") :].strip()
    parsed = handle_registry.parse(token)
    if parsed is not None and parsed[0] == "quest" and not parsed[1]:
        qid = parsed[2]
    elif token.isdigit():
        qid = int(token)
    else:
        raise BadInput(
            f"serves= takes a quest reference, got {serves!r}",
            next="args={'serves': 'quest:164903'}",
        )
    qref = store.get_ref(kind="quest", id=qid)
    if qref is None:
        raise NotFound(
            f"quest id={qid} not found",
            next="search(kind='quest', q='...') to find the quest",
        )
    return qref.id


def serving_ref_ids(store: Store, serves: Any) -> set[int]:
    """``args={'serves': 'quest:<id>'}`` → the live refs serving it —
    src ids of the quest's inbound ``serves`` edges."""
    links = store.links_for(
        serves_quest_id(store, serves), direction="in", relation="serves"
    )
    return {int(ln.src_ref_id) for ln in links}


__all__ = ["serves_quest_id", "serving_ref_ids"]
