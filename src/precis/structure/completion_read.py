"""Shared argument decoding and human/wire views for completion reads only.

External numeric selection/history arguments use the exact tagged wire codec;
internal callers use the typed reader directly. No policy or receipt proof can
be supplied through this boundary. No backend discovery or compute occurs.
"""

from __future__ import annotations

from typing import Any

from .completion import completion_digest, completion_envelope
from .completion_codec import dumps_wire, from_wire

STRUCTURE_ARGS = frozenset(
    {
        "requested_display",
        "shown_geometry_token",
        "selected_run_id",
        "history_limit",
        "request_limit",
    }
)


def read_structure_completion(
    store: Any, ref_id: int, args: dict[str, Any], *, digest: bool = False
) -> str | None:
    if set(args) - STRUCTURE_ARGS:
        raise ValueError("unsupported completion read argument")
    decoded = from_wire(args)
    shown = decoded.pop("shown_geometry_token", None)
    if shown is not None and type(shown) is not str:
        raise ValueError("shown_geometry_token must be a string")
    snapshot = store.structure_completion_snapshot(ref_id, **decoded)
    if snapshot is None:
        return None
    envelope = completion_envelope(snapshot, shown_geometry_token=shown)
    return completion_digest(envelope) if digest else dumps_wire(envelope)
