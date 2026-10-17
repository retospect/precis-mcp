"""``type='netlist_drc_clean'`` — pcb-guided-place-route geometric DRC gate.

Resolves ``True`` when the design's LATEST persisted geometric-DRC run
(``pcb_drc_findings``, :mod:`precis.pcb.drc`, written by ``op='check'``)
carries zero live ``severity='error'`` findings (a waived error does not
block — :func:`precis.pcb.route_state.route_state`'s one definition, which
the checklist's ``drc-clean`` item shares). A ``warn`` finding (inside JLC's
published minimum but eating into our house margin) does NOT block the
gate — a warn is manufacturable, just spending headroom, which is exactly
what the two-tier capability table is FOR (see ``capabilities.py``'s own
module docstring). ``None`` (leave the leaf open) when no DRC run has ever
been recorded for this board, or the latest predates its last edit —
"not yet checked" is not "clean".

Feeds the pcb-guided-place-route phase machine's final gate (Slice 10 of
docs/backlog/pcb-guided-place-route.md), alongside ``placement_legal`` and
``route_complete``. This evaluator never RUNS DRC itself (no shapely
import, no geometry) — it only reads the durable record a prior
``put(kind='pcb', args={'op':'check'})`` (or ``op='review'``) recorded —
the get views write nothing (gr491591) — the same "evaluator
reads, doesn't recompute" contract every other ``auto_check`` type here
follows.

Spec
====

```json
{"type": "netlist_drc_clean", "pcb": "sensor-node"}
```

``pcb`` names the design by slug (or accepts a bare ref id string/int).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.pcb.route_state import route_state
from precis.utils.slug_ref import resolve_live_slug_ref

if TYPE_CHECKING:
    from precis.store import Store


def _resolve_pcb_ref_id(store: Store, spec: dict[str, Any]) -> int:
    raw = spec.get("pcb")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise BadInput(
            "netlist_drc_clean needs a pcb design",
            next="meta.auto_check.pcb='sensor-node' (slug, or a ref id)",
        )
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise BadInput("netlist_drc_clean.pcb must be a slug string or ref id")
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if s.isdigit():
        return int(s)
    ref = resolve_live_slug_ref(store, kind="pcb", id=s)
    return ref.id


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    ref_id = _resolve_pcb_ref_id(store, spec)
    # None: no DRC run recorded yet, or the board changed since (gr492096)
    # — not yet, not a failure. A waived error does not block.
    return route_state(store, ref_id).drc_clean
