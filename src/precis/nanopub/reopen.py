"""Explicit local reopening for re-review, with an audit of any voided signature."""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.nanopub.state import TERMINAL

if TYPE_CHECKING:
    from precis.store import Store


def reopen(store: Store, hub_ref_id: int, *, reason: str, actor: str = "agent") -> int:
    """Return the reopened publish row's id."""
    if not reason.strip():
        raise BadInput(
            "reopen requires a non-empty reason", next="Supply reason='<why>'."
        )
    row = store.nanopub_publish_row(hub_ref_id)
    if row is None:
        posture = store.nanopub_publish_states_bulk([hub_ref_id]).get(hub_ref_id)
        if posture is not None and posture[0] in TERMINAL:
            raise BadInput(
                f"hub fi{hub_ref_id} is {posture[0]!r}; further action requires a human",
                next="Ask the claim owner to review its publication history.",
            )
        raise BadInput(
            f"hub fi{hub_ref_id} has no live publish row",
            next="Read the claim's publish history with view='publish-preflight'.",
        )
    if not store.nanopub_reopen(row.id, reason=reason.strip(), actor=actor):
        raise BadInput(
            f"publish row {row.id} disappeared",
            next="Read the claim's current state with view='publish-preflight'.",
        )
    return row.id
