"""Audited local successor staging; signing/publication are separate acts.

Only anchored, unpublished predecessors qualify. The store locks/rechecks the
exact row and atomically stages a linked candidate; no approved payload is
copied, so the ordinary review door will use the hub's current evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.nanopub.state import is_published_externally
from precis.store._nanopub_ops import PublishRow

if TYPE_CHECKING:
    from precis.store import Store


def supersede(store: Store, hub_ref_id: int, *, reason: str) -> PublishRow:
    """Stage a successor without keys, signing, calendars or registry access.

    ``reason`` is required and lands on a ``publish_superseded`` ref_event in
    the same transaction. A claim published externally is refused (human:
    ``precis nanopub retract``)."""
    if not reason or not reason.strip():
        raise BadInput("supersede needs a non-empty reason (the audit trail)")
    with store.tx() as conn:
        # Staging always locks refs before publish rows. Taking the publish
        # lock first would deadlock with create_publish_row or another stager.
        conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
            (hub_ref_id,),
        )
        old = store.nanopub_publish_row(hub_ref_id, conn=conn, for_update=True)
        if old is None:
            raise BadInput(f"hub fi{hub_ref_id} has no live publish row")
        if is_published_externally(old.state, old.published_at, old.registry_url):
            raise BadInput(
                f"fi{hub_ref_id} is {old.state!r} / published externally — an "
                "agent does not supersede a published claim",
                next="a human runs `precis nanopub retract` / `supersede`",
            )
        new = store.nanopub_supersede(old.id, conn=conn)
        store.append_event(
            hub_ref_id,
            source="nanopub",
            event="publish_superseded",
            payload={
                "from_state": old.state,
                "predecessor_id": old.id,
                "successor_id": new.id,
                "trusty_uri": old.trusty_uri,
                "artifact_id": old.artifact_id,
                "reason": reason.strip(),
            },
            conn=conn,
        )
    return new


def reopen(store: Store, hub_ref_id: int, *, reason: str) -> PublishRow:
    """Reopen a ``reviewed``/``signed`` row to ``candidate``, auditing the
    voided signature in the same transaction (``publish_reopened``)."""
    if not reason or not reason.strip():
        raise BadInput("reopen needs a non-empty reason (the audit trail)")
    with store.tx() as conn:
        old = store.nanopub_publish_row(hub_ref_id, conn=conn, for_update=True)
        if old is None:
            raise BadInput(f"hub fi{hub_ref_id} has no live publish row")
        if is_published_externally(old.state, old.published_at, old.registry_url):
            raise BadInput(
                f"fi{hub_ref_id} is {old.state!r} / published externally — "
                "reopening a published claim is a human act",
                next="a human runs `precis nanopub retract`",
            )
        if old.state == "anchored":
            raise BadInput(
                f"fi{hub_ref_id} is anchored — an anchor is irreversible, so "
                "stage a successor instead",
                next=f"edit(kind='finding', id='fi{hub_ref_id}', "
                "args={'supersede': True, 'reason': '<why>'})",
            )
        if old.state not in ("reviewed", "signed"):
            raise BadInput(
                f"fi{hub_ref_id} publish row is {old.state!r} — only "
                "reviewed/signed rows reopen"
            )
        if not store.nanopub_reopen(old.id, conn=conn):
            raise BadInput(f"publish row {old.id} changed state; retry")
        store.append_event(
            hub_ref_id,
            source="nanopub",
            event="publish_reopened",
            payload={
                "from_state": old.state,
                "trusty_uri": old.trusty_uri,
                "artifact_id": old.artifact_id,
                "reason": reason.strip(),
            },
            conn=conn,
        )
        new = store.nanopub_publish_row(hub_ref_id, conn=conn)
    assert new is not None
    return new
