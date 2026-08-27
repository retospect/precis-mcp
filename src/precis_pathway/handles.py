"""Universal handle code for the `pathway` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

precis-mcp's own kinds are the totality-tested SSOT in
``handle_registry.KIND_CODES``; a plugin kind (``pathway`` lives outside
precis-mcp) contributes its code here instead, merged in lazily. A pathway
ref gets the record code ``pw`` (``pw12``); it has no addressable body chunk
of its own (the methods-paragraph body chunk isn't separately handled), so
``CHUNK_CODES`` stays empty.
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"pathway": "pw"}
CHUNK_CODES: dict[str, str] = {}
