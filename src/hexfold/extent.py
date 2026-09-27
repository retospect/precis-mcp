"""Angstrom extents and the block's length anchors (SPEC 7, 12.1 0.2).

``sheet(W, H)`` takes its extent in lattice cells or in Angstrom
(``24.6A`` / ``24.6Å``).  An Angstrom extent is **snapped** to whole cells
along the lattice vector -- ``cells = max(1, round(x / a))``, ``a = √3·σ``
the lattice constant -- and the snap is *reported*, never silent:
``extent.snap`` (INFO) carries the requested length, the cells applied, the
realised length and the delta.  A hexfold sheet is a rhombic lattice patch
(``u`` cells along ``a₁``, ``v`` cells along ``a₂``, 60° apart), so the
extent is the length along each lattice vector, not a bounding box.

:func:`measures` turns a built net's discrete parameters back into the
length anchors the se side stacks tolerances against (se-kind L2,
``precis_se.measures.stackup``): each sheet's ``W``/``H`` and each tube's
``len`` with the **snap cell as its band** -- every request inside the band
lands on the same integer, so the band is exactly the tolerance the lattice
grants for free -- plus each tube's radius as a point.  Numpy-only, no
``precis*`` imports (``tests/test_hexfold_import_boundary.py``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any

from .lattice import Lattice, _lattice_metric_dot, translation_vector, tube_radius
from .report import Finding, Severity
from .text import Instance, Spec

_LEN_A_RE = re.compile(r"^\s*(?P<num>\d+(?:\.\d*)?|\.\d+)\s*(?:A|Å)\s*$")

#: (positional key, named key) for the two sheet extents
_SHEET_EXTENTS: tuple[tuple[str, str, str], ...] = (("0", "W", "W"), ("1", "H", "H"))


def parse_length_A(raw: str) -> float | None:
    """``"24.6A"`` / ``"24.6Å"`` -> 24.6; ``None`` when ``raw`` is not an
    Angstrom length (a bare integer is a cell count)."""
    m = _LEN_A_RE.match(raw)
    return float(m["num"]) if m else None


def snap_cells(length_A: float, period_A: float) -> int:
    """Whole periods nearest to ``length_A`` (never below 1)."""
    return max(1, round(length_A / period_A))


def snap_extents(spec: Spec, lat: Lattice) -> tuple[Spec, list[Finding]]:
    """Replace every Angstrom sheet extent by its snapped cell count and
    report each snap as ``extent.snap`` (INFO).  A spec with no Angstrom
    extents comes back unchanged with no findings."""
    findings: list[Finding] = []
    out: list[Instance] = []
    changed = False
    for inst in spec.instances:
        if inst.kind != "sheet":
            out.append(inst)
            continue
        params = list(inst.params)
        keys = {k for k, _ in params}
        for pos_key, name_key, label in _SHEET_EXTENTS:
            key = name_key if name_key in keys else pos_key
            idx = next((i for i, (k, _) in enumerate(params) if k == key), None)
            if idx is None:
                continue
            req = parse_length_A(params[idx][1])
            if req is None:
                continue
            cells = snap_cells(req, lat.a)
            realised = cells * lat.a
            params[idx] = (key, str(cells))
            changed = True
            findings.append(
                Finding(
                    "extent.snap",
                    Severity.INFO,
                    f"{inst.name}.{label}: {req:g} A snapped to {cells} cells "
                    f"= {realised:.3f} A (delta {realised - req:+.3f} A, "
                    f"a = {lat.a:.4f} A)",
                    where=inst.name,
                    span=inst.span,
                    data=(
                        ("cells", cells),
                        ("delta_A", round(realised - req, 4)),
                        ("param", label),
                        ("period_A", round(lat.a, 4)),
                        ("realised_A", round(realised, 4)),
                        ("requested_A", req),
                    ),
                )
            )
        out.append(replace(inst, params=tuple(params)) if changed else inst)
    if not changed:
        return spec, []
    return replace(spec, instances=tuple(out)), findings


@dataclass(frozen=True)
class Measure:
    """One length anchor of a built net, in Angstrom.  ``min_A``/``max_A``
    is the band of requests that snap to the same discrete value (``None``
    for a point such as a tube radius)."""

    name: str
    value_A: float
    min_A: float | None
    max_A: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_A": round(self.value_A, 4),
            "min_A": None if self.min_A is None else round(self.min_A, 4),
            "max_A": None if self.max_A is None else round(self.max_A, 4),
            "reason": self.reason,
        }


def _int_param(inst: Instance, pos: str, name: str, default: str) -> int | None:
    d = dict(inst.params)
    raw = d.get(name, d.get(pos, default))
    return int(raw) if raw.isdigit() else None


def measures(net: Any) -> list[Measure]:
    """The net's length anchors (module docstring): sheet ``<inst>_W`` /
    ``<inst>_H`` (value ``cells·a``, band ``±a/2``), tube ``<inst>_len``
    (value ``len·|T|``, band ``±|T|/2``) and ``<inst>_R`` (radius, point).
    Reads the *resolved* spec on the net, so ``len=fit``, domain fits and
    Angstrom snaps are already numbers.  Ordered by instance, then name."""
    lat: Lattice = net.lattice
    a = lat.a
    out: list[Measure] = []
    for inst in net.spec.instances:
        if inst.kind == "sheet":
            for pos, name, label in _SHEET_EXTENTS:
                cells = _int_param(inst, pos, name, "10")
                if cells is None:
                    continue
                out.append(
                    Measure(
                        f"{inst.name}_{label}",
                        cells * a,
                        (cells - 0.5) * a,
                        (cells + 0.5) * a,
                        f"hexfold sheet extent along the lattice vector: "
                        f"{cells} cells x a = {a:.4f} A; the band is the snap "
                        f"cell (requests inside it land on the same {cells} "
                        "cells)",
                    )
                )
        elif inst.kind == "tube":
            n = _int_param(inst, "0", "n", "5")
            m = _int_param(inst, "1", "m", "5")
            length = _int_param(inst, "2", "len", "1")
            if n is None or m is None:
                continue
            t = translation_vector(n, m)
            period = a * math.sqrt(_lattice_metric_dot(t, t))
            if length is not None:
                out.append(
                    Measure(
                        f"{inst.name}_len",
                        length * period,
                        (length - 0.5) * period,
                        (length + 0.5) * period,
                        f"hexfold tube({n},{m}) length: {length} periods x "
                        f"|T| = {period:.4f} A; the band is the snap period",
                    )
                )
            out.append(
                Measure(
                    f"{inst.name}_R",
                    tube_radius(n, m, lat),
                    None,
                    None,
                    f"hexfold tube({n},{m}) radius a*sqrt(n^2+nm+m^2)/2pi "
                    f"with a = {a:.4f} A (a point: fixed by the roll-up)",
                )
            )
    return out
