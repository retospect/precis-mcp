"""Shared shapes for the generator framework — split out from
``generators/__init__.py`` so :mod:`precis_se.atomic.generators.sp2` can import
them at module load time without a circular import (``__init__.py``
imports ``sp2`` itself, for the :data:`~precis_se.atomic.generators.GENERATORS`
registry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: The length unit every generator's envelope carries, **written into the
#: emitted DSL text**. Generator math is Å (the atomistic enclave —
#: bond lengths, vdW margins, cavity radii), the design side is SI metres,
#: and that crossing happens exactly once: the generator says ``Å``, and
#: :func:`precis.cad.dsl.parse`'s ``require_units=True`` grammar does the
#: multiply at the SAME ``add_block`` ingest boundary a hand-authored
#: envelope goes through (nm-se-merge.md: this replaced a handler-side
#: ``_envelope_A_to_m`` that re-parsed bare-number Å text and re-emitted
#: it in metres — a second conversion seam guarding nothing).
ENVELOPE_UNIT = "Å"


def fmt_length_A(x: float) -> str:
    """Render an ångström length as one unit-suffixed cad-DSL token value
    (``<key><number>Å``). Rounded to 4 decimals — 0.1 pm, well below any
    chemistry this side of the machine represents, and short enough that
    an envelope string stays readable in a view."""
    return f"{round(float(x), 4):g}{ENVELOPE_UNIT}"


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
    #: For a *ring* port (hexfold-style rim openings): the full dangling
    #: ring of atom indices this port can bond across — ``atom_index``
    #: stays atom 0 of that ring for back-compat binding. ``None`` for a
    #: single-atom port. Carried through the block's ``topology`` (the
    #: handler's ``add_port`` op has no column for it — the ring-port hook
    #: named in docs/backlog/hexfold-integration.md).
    atoms: list[int] | None = None


@dataclass
class GeneratedBlock:
    """Everything one ``generate`` op call needs from a generator.

    ``envelope`` is a ``precis.cad.dsl`` config string (the same
    vocabulary hand-built blocks already use — ``add_block``'s
    ``envelope`` reused verbatim, never a second grammar), with every
    length **unit-suffixed in Å** (:func:`fmt_length_A`) so the design side's
    one ingest boundary converts it.  ``topology``
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
    :mod:`precis_se.atomic.generators.sp2`'s module docstring for the fullerene
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
    #: Uniform ``Atom.hybridization`` tag the handler stamps on every
    #: realized atom (:meth:`precis_nm.handler.NmHandler._prepare_generate`)
    #: — round 1/2's sp² carbon families all hardcoded ``"sp2"`` before this
    #: field existed, so that stays the default; the sugars family
    #: (:mod:`precis_se.atomic.generators.sugars`) is the first sp³ family and
    #: sets ``"sp3"``.
    hybridization: str = "sp2"
    #: Per-atom hybridization override, parallel to ``elements`` — used by
    #: ``prepare_generate`` in place of the uniform :attr:`hybridization`
    #: when set (mixed-hybridization families: a hexfold net with sp³
    #: attachment sites stamps ``"sp2"``/``"sp3"`` per atom). ``None``
    #: keeps the uniform fallback.
    hybridizations: list[str] | None = None
    #: ``True`` for a check-only result (hexfold ``dry_run``): no atoms,
    #: no ports, ``provenance`` carries the rendered report, and
    #: ``prepare_generate`` returns a ``None`` pending — nothing is added
    #: to the tree or minted.
    dry_run: bool = False
