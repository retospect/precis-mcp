"""``type='route_complete'`` — pcb-guided-place-route routing gate.

Resolves ``True`` when every net on the design's board has reached
``pcb_routes.status = 'realized'`` — none left ``unrouted``, ``sketched``,
or ``failed``. A pure state read, the same shape as every other
``auto_check`` evaluator: the underlying guarantees this gate is named
for — every net realized, no unrouted connections, no unresolved
same-layer crossing left standing — are enforced by the ``pcb_route``
worker job at WRITE time, not recomputed here. A route whose net still has
a residual same-layer crossing (or an over-capacity gap the realizer
cannot clear) is written ``'failed'`` with the offending participants
named in ``pcb_routes.fail``, never ``'realized'`` — so "latest status is
realized" already means "this net actually routed cleanly."

Feeds the pcb-guided-place-route phase machine's routing gate (Slice 10 of
docs/backlog/pcb-guided-place-route.md; ADR 0042 Slice 9's
"route_complete" wording, superseded in shape by that spec's slice 10).

Spec
====

```json
{"type": "route_complete", "pcb": "sensor-node"}
```

``pcb`` names the design by slug (or accepts a bare ref id string/int). A
design with no nets at all resolves ``False`` (nothing to route is not
"routed").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._slug_ref_shared import resolve_live_slug_ref

if TYPE_CHECKING:
    from precis.store import Store


def _resolve_pcb_ref_id(store: Store, spec: dict[str, Any]) -> int:
    raw = spec.get("pcb")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise BadInput(
            "route_complete needs a pcb design",
            next="meta.auto_check.pcb='sensor-node' (slug, or a ref id)",
        )
    if isinstance(raw, bool):
        raise BadInput("route_complete.pcb must be a slug string or ref id")
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if s.isdigit():
        return int(s)
    ref = resolve_live_slug_ref(store, kind="pcb", id=s)
    return ref.id


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    ref_id = _resolve_pcb_ref_id(store, spec)
    rows = store.pcb_route_status(ref_id)
    if not rows:
        return False
    return all(r["status"] == "realized" for r in rows)
