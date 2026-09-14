"""Universal handle code for the `se` kind (precis.handle_codes entry
point, see ``precis.utils.handle_registry.PLUGIN_GROUP``).

An se design ref gets the record code ``se`` (``se234``) — same affordance
as its keystone siblings ``cad``/``structure``/``pcb``: se stores
real designs and joins cross-kind search, so its hits deserve a handle
(the gripe-278000 lesson, applied from day one this time). Blocks/ports
are addressed by name within a design, never per-row, so ``CHUNK_CODES``
stays empty — the same shape the (since merged, nm-se-merge.md) ``nm``
kind's own handle module used. ``se`` was verified free
in both ``KIND_CODES`` and ``CHUNK_CODES`` before claiming it
(se-kind.md "Decisions" — ``me`` was never available, it is the memory
kind's record code).
"""

from __future__ import annotations

RECORD_CODES: dict[str, str] = {"se": "se"}
CHUNK_CODES: dict[str, str] = {}
