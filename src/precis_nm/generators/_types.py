"""Shared shapes for the generator framework — split out from
``generators/__init__.py`` so :mod:`precis_nm.generators.sp2` can import
them at module load time without a circular import (``__init__.py``
imports ``sp2`` itself, for the :data:`~precis_nm.generators.GENERATORS`
registry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


class GeneratorError(ValueError):
    """A rejected generator call: bad params, or an unsupported family
    member (e.g. round 1's fullerene generator only covers the Goldberg
    (1,1) cage, C60 — a different atom count needs the general Goldberg
    construction, a later round). Always names the violated constraint and
    its valid range/formula — "theorems failing loudly"
    (docs/backlog/nm-kind.md "Generators"), never a silent clamp."""


@dataclass
class GeneratedPort:
    """One port on a generated block, keyed to the atom it will bind to.

    ``atom_index`` indexes into the *same* :class:`GeneratedBlock`'s
    ``elements``/``coords``/ordinal atom sequence — the handler-level
    ``generate`` op (:mod:`precis_nm.handler`) resolves it to the real
    atom label minted for that atom (in array order) in the freshly-minted
    ``structure`` design, and passes ``{port name: atom label}`` straight
    to ``bind_structure``, so the port is bound the moment the block is
    created — no separate "unbound generated port" state ever exists.
    There is no stored "position" field on a port (``nm_ports``'s schema
    carries ``direction`` only, no migration added for this slice, per the
    round-1 instruction) — the bound atom's coordinates in the structure
    design ARE the port's position, the "one fact, two projections" port
    model (pcb-component-model.md, transferred into nm-kind.md) applied
    here at the generator boundary rather than only at hand-built
    ``bind_structure`` time.
    """

    name: str
    atom_index: int
    direction: list[float]
    roles: list[str] = field(default_factory=lambda: ["covalent"])
    expected_element: str | None = None


@dataclass
class GeneratedBlock:
    """Everything one ``generate`` op call needs from a generator.

    ``envelope`` is a ``precis.cad.dsl`` config string (the same
    vocabulary hand-built blocks already use — ``add_block``'s
    ``envelope`` reused verbatim, never a second grammar).  ``topology``
    is the family's declared L2 invariant(s) (e.g. ``{"chiral_index":
    [n, m], "radius_A": ..., "pentagons": 0}`` for a nanotube,
    ``{"pentagons": 12, "hexagons": 20}`` for C60) — folded into the
    handler's echo this round; persisting it onto ``nm_topology`` is a
    later round (round 1 has nowhere in that table's shape for a
    scalar-valued invariant like a chiral index, only threading/chirality
    pairs — see ``0001_nm_kind.sql``). ``provenance`` is the formula/
    construction cite, stored verbatim as the minted block's ``desc``.
    ``elements``/``coords``/``bonds`` are the realized L5 atoms —
    ``coords`` a ``(N, 3)`` float64 Å array (ordinal index = array
    position = :attr:`GeneratedPort.atom_index`'s target), ``bonds`` a
    list of ``(i, j, order, kind)`` index quadruples into
    ``elements``/``coords`` — **order and kind are authoritative** (gripe
    279306): the handler-level ``generate`` op stores them verbatim on the
    minted :class:`~precis.structure.scene.Bond` rather than hardcoding a
    single aromatic order for every family. Each generator picks the
    chemically honest assignment for its own bond topology — see
    :mod:`precis_nm.generators.sp2`'s module docstring for the fullerene
    Kekule split and the CNT Pauling-order derivation — never a single
    "aromatic 1.5" guess that silently over-sums an all-sp² atom's valence
    budget (3 bonds × 1.5 = 4.5 > carbon's max valence of 4).
    """

    envelope: str
    ports: list[GeneratedPort]
    topology: dict[str, Any]
    provenance: str
    elements: list[str]
    coords: np.ndarray
    bonds: list[tuple[int, int, float, str]]
