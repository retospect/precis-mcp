"""Local successor staging; signing/publication are separate acts.

Only anchored, unpublished predecessors qualify. The store locks/rechecks the
exact row and atomically stages a linked candidate; no approved payload is
copied, so the ordinary review door will use the hub's current evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store._nanopub_ops import PublishRow


def supersede(
    store: Store, hub_ref_id: int, *, reason: str, actor: str = "agent"
) -> PublishRow:
    """Stage a successor without keys, signing, calendars or registry access."""
    if not reason.strip():
        raise BadInput(
            "supersede requires a non-empty reason", next="Supply reason='<why>'."
        )
    old = store.nanopub_publish_row(hub_ref_id)
    if old is None:
        raise BadInput(
            f"hub fi{hub_ref_id} has no live publish row",
            next="Read the claim's publish history with view='publish-preflight'.",
        )
    return store.nanopub_supersede(old.id, reason=reason.strip(), actor=actor)
