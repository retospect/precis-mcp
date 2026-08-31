"""Parametric block factories — the IC-design PCell, imported
(docs/backlog/nm-kind.md "Generators — parametric block factories"). A
generator is a pure function ``params → block`` (:class:`GeneratedBlock`):
for chemistry families where the math fixes the atoms (a ``(n, m)``
nanotube's radius, a fullerene's Goldberg-construction vertex count), the
LLM never guesses a single coordinate — the closed-form geometry runs
instead, deterministic and reproducible. This is the *first* of the three
fill paths nm-kind.md's "Generators" section orders (generator →
``nm_propose`` LLM job → hand ops): generators shrink what the LLM must
invent.

Every generator here is **pure** — no store access, the ``ops.py``
discipline — taking a JSON-shaped ``params`` dict and returning a
:class:`GeneratedBlock` (:mod:`precis_nm.generators._types`) carrying
everything the handler-level ``generate`` op
(:meth:`precis_nm.handler.NmHandler._generate`, the same
``import_fragment``/``bind_structure`` store-aware-interception pattern
``ops.py``'s module docstring already documents) needs to (1) add a new
block with the generated envelope, (2) add its ports, (3) mint a
``structure`` design holding the realized atoms/bonds, and (4) bind it —
never touching the store itself.

**Param validation is theorems failing loudly** (nm-kind.md): a rejected
parameter set raises :class:`~precis_nm.generators._types.GeneratorError`
naming the violated constraint and its valid range/formula, before any
geometry runs — never a silent clamp or a NaN downstream (see
:mod:`precis_nm.generators.sp2`'s module docstring for both families'
derivations). Every generator's build function also carries a provenance
note (the formula/construction used) into the returned block's
``provenance`` field, which the handler stores as the minted block's
``desc``.

:data:`GENERATORS` is the name → builder registry the ``generate`` op
looks up. Round 1 (slice 4a, build order (i)): ``cnt`` (single-wall carbon
nanotube, chiral rolling) and ``fullerene`` (C60, truncated icosahedron) —
both in :mod:`precis_nm.generators.sp2`. Round 2 (build order (ii)):
``cone`` (nanohorn, wrapped-sheet disclination — also
:mod:`precis_nm.generators.sp2`). Nanobud fusion and cyclodextrin remain
build order (ii); L4 mechanics-ceiling metrics are build order (iii)
(nm-kind.md's slice 4a entry).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis_nm.generators._types import GeneratedBlock, GeneratedPort, GeneratorError
from precis_nm.generators.sp2 import build_cnt, build_cone, build_fullerene

Generator = Callable[[dict[str, Any]], GeneratedBlock]

#: name → builder, looked up by the ``generate`` op
#: (:meth:`precis_nm.handler.NmHandler._generate`). An unknown name is a
#: loud ``BadInput`` listing every registered name — the handler does that
#: lookup and the error mapping, not this module.
GENERATORS: dict[str, Generator] = {
    "cnt": build_cnt,
    "fullerene": build_fullerene,
    "cone": build_cone,
}

__all__ = [
    "GENERATORS",
    "GeneratedBlock",
    "GeneratedPort",
    "Generator",
    "GeneratorError",
]
