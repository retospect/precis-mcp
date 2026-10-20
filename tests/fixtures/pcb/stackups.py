"""Named stackups the pcb tests pin explicitly."""

from __future__ import annotations

from typing import Any

#: The v1 SIG/GND/PWR/SIG default every board was born with until
#: gr495395 (and migration 0138's backfill literal): two routing layers,
#: In1.Cu a GND ``plane_net`` declaration, In2.Cu a bare plane. Tests of
#: plane-layer behaviour (plane promotion, legacy declarations, a layer
#: the stackup does not route on) pin it, since the default no longer has
#: plane layers.
LEGACY_V1_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
    {"name": "In2.Cu", "role": "plane"},
    {"name": "B.Cu", "role": "signal"},
]
