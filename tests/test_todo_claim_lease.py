"""Claim-lease semantics on the todo tree (``claimed-by:`` as a CAS lease).

Before the lease, ``claimed-by:<x>`` was an ordinary open tag: two agents
could both add one (no mutual exclusion), a claimed leaf still appeared in
``view='doable'`` (every fleet member was offered already-claimed work),
and a dead claimer's tag lingered forever. The lease closes all three:
CAS on claim (``guards.check_claim_takeover``), doable/dispatch exclusion
while the lease is live (``_doable_exclusion_clause``), expiry via the
``ref_tags.expires_at`` column (stamped + refreshed in
``TodoHandler._after_tag_mutation``), and release on terminal STATUS.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.store import Store


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    from tests.conftest import id_of

    return id_of(body)


def _leaf(handler: TodoHandler, title: str) -> int:
    root = handler.put(text=f"Strategic for {title}", meta={"rotation_root": True})
    leaf = handler.put(text=title, parent_id=_id_of(root.body))
    return _id_of(leaf.body)


def _claim_expiry(store: Store, ref_id: int) -> tuple[str, datetime | None] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value, rt.expires_at FROM ref_tags rt"
            " JOIN tags t ON t.tag_id = rt.tag_id"
            " WHERE rt.ref_id = %s AND t.namespace = 'OPEN'"
            "   AND t.value LIKE 'claimed-by:%%'",
            (ref_id,),
        ).fetchall()
    assert len(row) <= 1, f"single-holder invariant violated: {row}"
    return (str(row[0][0]), row[0][1]) if row else None


def _age_claim(store: Store, ref_id: int, sql_expiry: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            f"UPDATE ref_tags rt SET expires_at = {sql_expiry}"
            "  FROM tags t"
            " WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s"
            "   AND t.namespace = 'OPEN' AND t.value LIKE 'claimed-by:%%'",
            (ref_id,),
        )
        conn.commit()


def test_claim_gets_a_lease_and_excludes_from_doable(
    handler: TodoHandler, store: Store
) -> None:
    leaf = _leaf(handler, "Lease me.")
    assert "Lease me." in handler.search(view="doable").body

    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    claim = _claim_expiry(store, leaf)
    assert claim is not None
    assert claim[0] == "claimed-by:agent-a"
    assert claim[1] is not None  # lease stamped, not an open-ended tag
    assert "Lease me." not in handler.search(view="doable").body


def test_expired_lease_resurfaces_in_doable(handler: TodoHandler, store: Store) -> None:
    leaf = _leaf(handler, "Orphaned by a dead claimer.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    _age_claim(store, leaf, "now() - interval '1 minute'")
    assert "Orphaned by a dead claimer." in handler.search(view="doable").body


def test_second_claimer_rejected_while_lease_live(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = _leaf(handler, "Contested leaf.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker-b")
    with pytest.raises(BadInput, match="already claimed by 'agent-a'"):
        handler.tag(id=leaf, add=["claimed-by:agent-b"])


def test_expired_lease_is_free_to_take_over(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = _leaf(handler, "Takeover after expiry.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    _age_claim(store, leaf, "now() - interval '1 minute'")
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker-b")
    handler.tag(id=leaf, add=["claimed-by:agent-b"])
    claim = _claim_expiry(store, leaf)
    assert claim is not None and claim[0] == "claimed-by:agent-b"


def test_owner_takeover_replaces_a_live_claim(
    handler: TodoHandler, store: Store
) -> None:
    # Default test source is 'cli' → owner authority: may override a
    # live lease; the old holder's row is dropped (single-holder).
    leaf = _leaf(handler, "Owner override.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    handler.tag(id=leaf, add=["claimed-by:reto"])
    claim = _claim_expiry(store, leaf)
    assert claim is not None and claim[0] == "claimed-by:reto"


def test_reclaim_refreshes_the_lease(handler: TodoHandler, store: Store) -> None:
    leaf = _leaf(handler, "Long-running claimer.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    _age_claim(store, leaf, "now() + interval '1 minute'")  # nearly expired
    handler.tag(id=leaf, add=["claimed-by:agent-a"])  # re-assert
    claim = _claim_expiry(store, leaf)
    assert claim is not None and claim[1] is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT %s::timestamptz > now() + interval '3 hours'",
            (claim[1],),
        ).fetchone()
    assert row is not None and row[0], "re-claim must extend the lease"


def test_terminal_status_releases_the_claim(handler: TodoHandler, store: Store) -> None:
    leaf = _leaf(handler, "Finished leaf.")
    handler.tag(id=leaf, add=["claimed-by:agent-a"])
    handler.tag(id=leaf, add=["STATUS:done"])
    assert _claim_expiry(store, leaf) is None


def test_legacy_null_expiry_claim_does_not_exclude(
    handler: TodoHandler, store: Store
) -> None:
    """A claim row minted before the lease semantics (``expires_at`` NULL)
    keeps its pre-lease behaviour: visible marker, but NOT a doable
    exclusion — old stale claims must not suddenly park live leaves."""
    leaf = _leaf(handler, "Legacy claim.")
    handler.tag(id=leaf, add=["claimed-by:old-agent"])
    _age_claim(store, leaf, "NULL")
    assert "Legacy claim." in handler.search(view="doable").body
