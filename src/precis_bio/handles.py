"""Universal handle code for the `protein` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

precis-mcp's own kinds are the totality-tested SSOT in
``handle_registry.KIND_CODES``; a plugin kind (``protein`` lives outside
precis-mcp) contributes its code here instead, merged in lazily. A protein
ref gets the record code ``pi`` (``pi12``) — first+later-letter, the
obvious ``p`` + {``r``,``o``,``t``,``n``} combos are all already taken
(``pr`` is ``pres``, ``po`` is ``plan``, ``pt`` is ``patent``, ``pn`` is
``part``); ``pi`` was verified free against ``KIND_CODES``, ``CHUNK_CODES``,
and the plugin codes registered at the time (``pw``/``es``/``nm``/``se`` —
``nm`` has since merged into ``se``, gr329871's check predates that)
before claiming it. Residues are addressed by position within a
sequence, never per-row, so ``CHUNK_CODES`` stays empty — mirrors
``precis_se/handles.py``.
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"protein": "pi"}
CHUNK_CODES: dict[str, str] = {}
