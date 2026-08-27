"""Universal handle code for the `estimate` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

precis-mcp's own kinds are the totality-tested SSOT in
``handle_registry.KIND_CODES``; a plugin kind (``estimate`` lives outside
precis-mcp) contributes its code here instead, merged in lazily. An estimate
ref gets the record code ``es`` (``es12``); the panel is a single cached
body block addressed at the ref level (not per-block), so ``CHUNK_CODES``
stays empty — mirrors ``precis_pathway/handles.py``.
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"estimate": "es"}
CHUNK_CODES: dict[str, str] = {}
