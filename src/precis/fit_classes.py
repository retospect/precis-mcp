"""Clearance-hole **fit classes** — how big a hole a given thread passes
through (docs/backlog/se-off-the-shelf-fabrication.md, engine 2).

A fit class is the one number that turns a `screw` mechanism into
geometry: an M6 does not pass through a 6.0 mm hole, and *which* larger
number you pick is a shop decision, not a property of the screw. Two
tiers, and they are different kinds of fact:

- **The table is standards data** — ISO 273 (= DIN EN 20273) fine /
  medium / coarse, keyed by thread size. A published table, so it lives
  in `precis/data/fit_classes.json` beside :mod:`precis.component_series`
  and is changed by a commit, not a migration.
- **The class you build to is a house/process choice** — the default here
  is ``house``, Reto's shop rule of ``d + 0.2`` (M6 → 6.2). It is a
  *rule*, not a column, so it applies at every size including ones the
  ISO table doesn't list.

Everything in the file is millimetres (one unit for the whole file, the
`component_series.json` convention). This module hands back a
:class:`Fit` rather than a bare float precisely because se is metres
everywhere: a field named ``hole_mm`` cannot be mistaken for one named
``hole_m``, which is the mistake that would silently drill a 6-metre hole.

Pure: no store, no network, no unit table beyond its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

_PACKAGED_DATA = "precis.data"
_FILE = "fit_classes.json"


@dataclass(frozen=True)
class Fit:
    """One resolved clearance hole. ``hole_mm``/``nominal_mm`` name their
    unit on purpose (module docstring); ``source`` is the provenance of
    *this* number, so a house rule never reads as a standard."""

    thread_size: str
    fit_class: str
    hole_mm: float
    nominal_mm: float
    source: str

    @property
    def hole_m(self) -> float:
        return self.hole_mm / 1000.0

    @property
    def radial_slack_mm(self) -> float:
        """Half the diametral clearance — the position error one hole of a
        multi-hole pattern can absorb before the pattern binds."""
        return (self.hole_mm - self.nominal_mm) / 2.0


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _norm_size(thread_size: str) -> str:
    """``'m6'``/``' M6 '`` → ``'M6'``. Sizes are written ``M6`` in every
    table we read, so one spelling is enough — this only absorbs case and
    stray whitespace, not a general designation parser
    (:func:`precis.component_series.split_designation` is that)."""
    return thread_size.strip().upper()


def default_class() -> str:
    """The class used when a design doesn't name one."""
    return str(_data().get("default_class") or "house")


@lru_cache(maxsize=1)
def classes() -> dict[str, dict[str, Any]]:
    """Every fit class by id, each with its ``title``/``source`` and either
    an ``offset_mm`` rule or nothing (tabulated per size)."""
    raw: dict[str, Any] = _data().get("classes") or {}
    return {str(k): dict(v) for k, v in raw.items()}


@lru_cache(maxsize=1)
def _sizes() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _data().get("sizes") or []:
        out[_norm_size(str(row["thread_size"]))] = dict(row)
    return out


def sizes() -> list[str]:
    """Thread sizes the ISO table covers, small to large."""
    return sorted(_sizes(), key=lambda s: float(_sizes()[s]["nominal_diameter"]))


def clearance_hole(thread_size: str, fit_class: str | None = None) -> Fit | None:
    """The clearance hole for ``thread_size`` in ``fit_class``, or ``None``
    when either is unknown.

    ``None`` is the honest answer — a caller that got a plausible number
    for an unlisted thread would stamp an invented dimension into a real
    part. Callers report the gap by name; they never fall back to the
    nominal diameter, which would produce a hole the screw cannot pass."""
    size = _sizes().get(_norm_size(thread_size))
    if size is None:
        return None
    cls_id = (fit_class or default_class()).strip().lower()
    cls = classes().get(cls_id)
    if cls is None:
        return None
    nominal = float(size["nominal_diameter"])
    offset = cls.get("offset_mm")
    if offset is not None:
        hole = nominal + float(offset)
    else:
        tabulated = (size.get("holes") or {}).get(cls_id)
        if tabulated is None:
            return None
        hole = float(tabulated)
    return Fit(
        thread_size=_norm_size(thread_size),
        fit_class=cls_id,
        hole_mm=hole,
        nominal_mm=nominal,
        source=str(cls.get("source") or ""),
    )
