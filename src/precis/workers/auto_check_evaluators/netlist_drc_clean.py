"""``type='netlist_drc_clean'`` — pcb-guided-place-route geometric DRC gate.

Resolves ``True`` when the design's LATEST persisted geometric-DRC run
(``pcb_drc_findings``, :mod:`precis.pcb.drc`, written by ``view='drc'``)
carries zero ``severity='error'`` findings. A ``warn`` finding (inside JLC's
published minimum but eating into our house margin) does NOT block the
gate — a warn is manufacturable, just spending headroom, which is exactly
what the two-tier capability table is FOR (see ``capabilities.py``'s own
module docstring). ``None`` (leave the leaf open) when no DRC run has ever
been recorded for this board — "not yet checked" is not "clean".

Feeds the pcb-guided-place-route phase machine's final gate (Slice 10 of
docs/backlog/pcb-guided-place-route.md), alongside ``placement_legal`` and
``route_complete``. This evaluator never RUNS DRC itself (no shapely
import, no geometry) — it only reads the durable record a prior
``get(kind='pcb', view='drc')`` call already wrote, the same "evaluator
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
from precis.handlers._slug_ref_shared import resolve_live_slug_ref

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
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    if run_id is None:
        return None  # no DRC run recorded yet — not yet, not a failure
    return not any(f["severity"] == "error" for f in findings)
