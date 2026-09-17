"""**Threads into soft material** — what a screw meets at the far end of
a stack when the member it lands in was printed
(docs/backlog/se-off-the-shelf-fabrication.md, engine 2b / rung 3c).

The sibling of :mod:`precis.fit_classes`, and the split is worth naming:
fit classes answer *how big a hole this screw passes through*, which is a
published standard (ISO 273). This module answers *what the screw threads
into*, which mostly isn't — a cut thread in steel is arithmetic (d − P),
and everything about a thread in thermoplastic is a manufacturer's rule
of thumb. So the data file marks, per row, which kind of number it is,
and this module hands back a :class:`Feature` that carries the source
sentence with it. Nothing here should be able to produce a plausible
number whose provenance has gone missing.

**Five strategies, and the choice is declared, never guessed**
(``joint.params.thread_strategy``):

- ``nut`` — clearance through, a nut on the far side. No thread touches
  the plastic.
- ``nut-trap`` — a hex pocket holds a steel nut captive.
- ``insert`` — a heat-set brass insert in a stepped pocket.
- ``thread-forming`` — a pointy screw deforms a core hole into a thread.
- ``tapped`` — a cut thread. Right in metal; a short-lived choice in
  plastic, which is why it is no longer the silent default there.

Millimetres throughout the file, like every other curated data file here;
:class:`Feature` names its unit in the field (``diameter_mm`` beside
``diameter_m``) for the same reason :class:`precis.fit_classes.Fit` does
— se is metres everywhere, and a bare float is how a 4 mm hole becomes a
4 m one.

Pure: no store, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

_PACKAGED_DATA = "precis.data"
_FILE = "thread_forming.json"

#: The declared strategies, in the order a chooser should prefer them for
#: a printed member: strongest first, cheapest last. Ordered so a finding
#: can list the alternatives in a useful order rather than alphabetically.
STRATEGIES: tuple[str, ...] = ("nut", "nut-trap", "insert", "thread-forming", "tapped")


@dataclass(frozen=True)
class Feature:
    """One stamped feature's size. ``kind`` is the hole vocabulary
    (``'core'``, ``'tapped'``, ``'insert-pocket'``, ``'nut-pocket'``);
    ``depth_mm`` is ``None`` when the strategy does not fix a depth (a
    clearance hole goes all the way through whatever it is in)."""

    kind: str
    diameter_mm: float
    depth_mm: float | None
    source: str
    #: Across-flats for a hex pocket — a nut trap is not round, and a
    #: diameter alone would describe a hole that lets the nut spin.
    across_flats_mm: float | None = None
    #: Lead-in chamfer at the mouth, where the strategy wants one.
    chamfer_mm: float | None = None

    @property
    def diameter_m(self) -> float:
        return self.diameter_mm / 1000.0

    @property
    def depth_m(self) -> float | None:
        return None if self.depth_mm is None else self.depth_mm / 1000.0


@dataclass(frozen=True)
class MaterialRule:
    """The per-material numbers a printed thread needs.
    ``core_hole_factor`` is ``None`` for metal — a cut thread's drill is
    d − P, not a fraction of the major diameter."""

    key: str
    title: str
    core_hole_factor: float | None
    min_engagement_d: float
    boss_outer_factor: float
    min_boss_wall_mm: float
    source: str


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _norm(thread_size: str) -> str:
    return str(thread_size).strip().upper()


def default_material() -> str:
    return str(_data().get("default_material_class") or "thermoplastic-rigid")


def strategies() -> dict[str, dict[str, str]]:
    """Every strategy by id, with its ``title``/``stamps``/``note`` — the
    prose a view shows when it refuses to guess for the designer."""
    raw: dict[str, Any] = _data().get("strategies") or {}
    return {str(k): {kk: str(vv) for kk, vv in v.items()} for k, v in raw.items()}


@dataclass(frozen=True)
class BlindHoleRule:
    """How much deeper than the engaged thread a **blind** tapped or
    thread-forming hole is drilled — there is no ISO for this, it is shop
    practice (``blind_hole.source``): tip clearance so the screw never
    bottoms on thread runout, plus (for a cut thread only) a tap-chamfer
    allowance for the plug tap's lead-in."""

    tip_clearance_pitches: float
    tap_chamfer_pitches: float
    source: str


def blind_hole() -> BlindHoleRule:
    """The blind-hole depth rule, in pitches (there is no diameter or
    material dependence here, unlike every other section of this file)."""
    row: dict[str, Any] = _data().get("blind_hole") or {}
    return BlindHoleRule(
        tip_clearance_pitches=float(row.get("tip_clearance_pitches") or 0.0),
        tap_chamfer_pitches=float(row.get("tap_chamfer_pitches") or 0.0),
        source=str(row.get("source") or ""),
    )


@lru_cache(maxsize=1)
def _materials() -> dict[str, MaterialRule]:
    out: dict[str, MaterialRule] = {}
    for key, row in (_data().get("materials") or {}).items():
        factor = row.get("core_hole_factor")
        out[str(key)] = MaterialRule(
            key=str(key),
            title=str(row.get("title") or key),
            core_hole_factor=None if factor is None else float(factor),
            min_engagement_d=float(row["min_engagement_d"]),
            boss_outer_factor=float(row["boss_outer_factor"]),
            min_boss_wall_mm=float(row["min_boss_wall_mm"]),
            source=str(row.get("source") or ""),
        )
    return out


def material(key: str | None) -> MaterialRule | None:
    """One material rule, or ``None`` for a class nobody has characterized
    — which a caller reports rather than papering over with the default:
    silently applying the rigid-plastic factor to an unknown material is
    how an invented number gets into a real boss."""
    return _materials().get(str(key or default_material()).strip().lower())


@lru_cache(maxsize=1)
def _cores() -> dict[str, dict[str, Any]]:
    return {
        _norm(row["thread_size"]): dict(row)
        for row in _data().get("thread_cores") or []
    }


def thread_sizes() -> list[str]:
    """Sizes the core table covers — metric machine threads and the ST
    tapping-screw ladder together, since a joint may use either."""
    return sorted(_cores(), key=lambda s: float(_cores()[s]["major"]))


def core_hole(thread_size: str, *, material_class: str | None = None) -> Feature | None:
    """The hole a **thread-forming** screw wants, or ``None`` when the
    size or material is unknown.

    ``factor × major``, floored at the thread's own minor diameter: a hole
    smaller than the minor Ø is not a tighter fit, it is a screw that
    cannot go in without splitting the boss. The floor is the transcribed
    half of the answer and the factor is the rule-of-thumb half, so the
    returned ``source`` names both."""
    row = _cores().get(_norm(thread_size))
    rule = material(material_class)
    if row is None or rule is None or rule.core_hole_factor is None:
        return None
    major = float(row["major"])
    minor = float(row["minor"])
    hole = max(rule.core_hole_factor * major, minor)
    return Feature(
        kind="core",
        diameter_mm=round(hole, 2),
        depth_mm=None,
        source=(
            f"{rule.core_hole_factor:g} × major Ø, floored at the minor Ø "
            f"{minor:g} mm ({row.get('source')}); factor: {rule.source}"
        ),
    )


def tapping_drill(thread_size: str, pitch_mm: float) -> Feature | None:
    """The cut-thread drill, ``d − P``. Arithmetic, not a table — but it
    still comes back as a :class:`Feature` so every stamped hole in the
    tree has the same shape and the same provenance sentence."""
    row = _cores().get(_norm(thread_size))
    if row is None or pitch_mm <= 0.0:
        return None
    return Feature(
        kind="tapped",
        diameter_mm=round(float(row["major"]) - pitch_mm, 2),
        depth_mm=None,
        source="d − P, the ~100% thread-form tapping drill (ISO 261)",
    )


def insert_series_id() -> str:
    """The `component` series the insert pockets are sized for. Named here
    rather than hardcoded at the call site, so the pocket table and the
    part it accepts can never drift apart silently."""
    return str((_data().get("inserts") or {}).get("series_id") or "")


def insert_pocket(thread_size: str, *, insert_length_mm: float) -> Feature | None:
    """The stepped pocket a heat-set insert melts into, or ``None`` for a
    size the insert table doesn't carry."""
    data = _data().get("inserts") or {}
    rows = {_norm(r["thread_size"]): r for r in data.get("sizes") or []}
    row = rows.get(_norm(thread_size))
    if row is None or insert_length_mm <= 0.0:
        return None
    return Feature(
        kind="insert-pocket",
        diameter_mm=float(row["pocket_diameter"])
        + float(data.get("pocket_clearance_mm") or 0.0),
        depth_mm=insert_length_mm + float(data.get("pocket_depth_over_mm") or 0.0),
        chamfer_mm=float(data.get("lead_in_chamfer_mm") or 0.0) or None,
        source=str(data.get("source") or ""),
    )


def nut_pocket(*, across_flats_mm: float, nut_height_mm: float) -> Feature:
    """The hex pocket for a captive nut. Always answerable — its inputs
    are the nut's own dimensions, which the series row carries — so this
    returns a feature rather than an optional."""
    data = _data().get("nut_trap") or {}
    af = across_flats_mm + float(data.get("across_flats_clearance_mm") or 0.0)
    return Feature(
        kind="nut-pocket",
        # The circumscribed circle: a hex pocket's bounding diameter is
        # what a clearance check cares about, and across_flats_mm carries
        # the shape.
        diameter_mm=af * 2.0 / 3.0**0.5,
        depth_mm=nut_height_mm + float(data.get("depth_clearance_mm") or 0.0),
        across_flats_mm=af,
        source=str(data.get("source") or ""),
    )


def boss(thread_size: str, *, material_class: str | None = None) -> Feature | None:
    """The boss a thread-forming screw wants around its core hole: outer
    diameter ``factor × major``, which is the wall thickness the hoop
    stress of a formed thread needs. ``depth_mm`` is left ``None`` —
    how *deep* the boss runs is the design's business (it follows the
    engagement), while how *thick* it is, is the material's."""
    row = _cores().get(_norm(thread_size))
    rule = material(material_class)
    if row is None or rule is None:
        return None
    major = float(row["major"])
    outer = max(rule.boss_outer_factor * major, major + 2.0 * rule.min_boss_wall_mm)
    return Feature(
        kind="boss",
        diameter_mm=round(outer, 2),
        depth_mm=None,
        source=(
            f"{rule.boss_outer_factor:g} × major Ø, at least "
            f"{rule.min_boss_wall_mm:g} mm of wall each side — {rule.source}"
        ),
    )
