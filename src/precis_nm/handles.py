"""Universal handle code for the `nm` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

An nm design ref gets the record code ``nm`` (``nm12``) — same affordance
as its keystone siblings ``cad``/``structure``/``pcb`` (gripe 278000: the
round-1 skeleton skipped this, following the lighter refs.meta-only
plugins, but nm stores real designs and joins cross-kind search, so its
hits deserve a handle). Blocks/ports are addressed by name within a
design, never per-row, so ``CHUNK_CODES`` stays empty — mirrors
``precis_estimate/handles.py``.
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"nm": "nm"}
CHUNK_CODES: dict[str, str] = {}
