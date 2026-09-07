"""Universal handle code for the `route` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

precis-mcp's own kinds are the totality-tested SSOT in
``handle_registry.KIND_CODES``; a plugin kind (``route`` lives outside
precis-mcp) contributes its code here instead, merged in lazily. A route
ref gets the record code ``rt`` (``rt12``) — first+third letter, since the
literal ``pr``/``rt``-adjacent 2-letter combos already taken (``patent`` is
``pt``) forced picking around the vowel. ``rt`` was verified free against
``KIND_CODES``, ``CHUNK_CODES``, and the four registered plugin codes
(``pw``/``es``/``nm``/``se``) before claiming it (gr329871). Route steps
are addressed by position within the route, never per-row, so
``CHUNK_CODES`` stays empty — mirrors ``precis_pathway/handles.py``.
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"route": "rt"}
CHUNK_CODES: dict[str, str] = {}
