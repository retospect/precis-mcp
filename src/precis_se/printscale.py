"""Print scale: parameter validation, the suggested factor and the floor seam.

A scaled print file multiplies a block's geometry by a dimensionless factor
(``precis:scale_factor`` in the 3MF); the checks run at the printed size
(:func:`precis_se.printing.report_for` ``scale=``). This module owns what
the web route and the ``view='print'`` handler share around that:

* :func:`parse_scale` / :func:`parse_model` — one validation, one error
  vocabulary (:class:`PrintParamError`);
* :func:`floor_for` — the smallest factor that prints. A solid block has no
  floor (``None``); an atom model's strut floor is supplied by
  printable-atomic-models, which replaces the body here;
* :func:`suggest_scale` — fit the longest side to :data:`FIT_MM`, rounded
  DOWN to a 1-2-5 step, never below the floor;
* :func:`descriptor` — the ``.json`` payload the dialog reads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad.export import format_scale_factor
from precis_se.printsolid import PrintedSolid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis_se.ops import SeTree

#: Atom print models a request may name (``model=``). They stay prospective
#: until printable-atomic-models supplies geometry: no block advertises them.
KNOWN_MODELS = ("vdw", "ballstick")

#: A block whose longest side is below this (mm) is suggested a scale-up;
#: at or above it the suggestion is 1:1.
ONE_TO_ONE_FROM_MM = 1.0
#: Target longest side (mm) of a scaled-up, or reduced, print.
FIT_MM = 100.0
#: Longest side (mm) beyond which a 1:1 print no longer fits the printer and
#: a reduction is suggested.
BED_MM = 256.0

ATOM_UNAVAILABLE = (
    "atomic block {block!r} is not printable yet — printable atomic models "
    "have not shipped"
)


class PrintParamError(ValueError):
    """A malformed print parameter (``scale=``/``model=``). ``code`` is the
    wire error (``invalid_scale`` | ``unsupported_model``)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class BelowFloor(Exception):
    """``scale`` is below the selected model's printable floor."""

    def __init__(self, min_scale: float) -> None:
        super().__init__(f"scale is below the printable floor {min_scale:g}")
        self.min_scale = min_scale


def parse_scale(raw: str | float | int | None) -> float:
    """``raw`` (query string or handler arg) -> a finite positive factor;
    ``None`` / empty is 1. Raises :class:`PrintParamError` ``invalid_scale``."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 1.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise PrintParamError(
            "invalid_scale", f"scale must be a number, got {raw!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise PrintParamError(
            "invalid_scale", f"scale must be finite and positive, got {raw!r}"
        )
    return value


def is_atomic(tree: SeTree, block: str) -> bool:
    """Whether ``block`` is bound to an atomic ``structure`` ref."""
    return tree.blocks[block].bound_kind == "structure"


def parse_model(tree: SeTree, block: str, raw: str | None) -> str | None:
    """Validate ``model=``: absent -> ``None``; a name outside
    :data:`KNOWN_MODELS`, or any model on a solid (non-atomic) block, raises
    :class:`PrintParamError` ``unsupported_model``."""
    if raw is None or not raw.strip():
        return None
    model = raw.strip().lower()
    if model not in KNOWN_MODELS:
        raise PrintParamError(
            "unsupported_model",
            f"unknown model {raw!r}; known atom models: {', '.join(KNOWN_MODELS)}",
        )
    if not is_atomic(tree, block):
        raise PrintParamError(
            "unsupported_model",
            f"model= applies to atomic blocks only; {block!r} is a solid",
        )
    return model


def floor_for(tree: SeTree, block: str, model: str | None) -> float | None:
    """The smallest scale at which ``block`` prints with ``model``, or
    ``None`` when there is no floor. A solid has none (a too-small solid is
    reported by the print check at printed size, not refused). The atom
    models' strut floors arrive with printable-atomic-models, which
    replaces this body."""
    return None


def check_floor(tree: SeTree, block: str, model: str | None, scale: float) -> None:
    """Raise :class:`BelowFloor` when ``scale`` is under the floor."""
    floor = floor_for(tree, block, model)
    if floor is not None and scale < floor:
        raise BelowFloor(floor)


def _round_down_125(x: float) -> float:
    """``x`` rounded DOWN to a 1-2-5 step (…1e7, 2e7, 5e7…)."""
    x = float(f"{x:.12g}")  # absorb 100/5e-6 = 20000000.000000004
    exp = math.floor(math.log10(x))
    mantissa = x / 10.0**exp
    step = 5 if mantissa >= 5 else 2 if mantissa >= 2 else 1
    return float(f"{step}e{exp}")


def suggest_scale(
    real_size_mm: list[float] | None, min_scale: float | None
) -> float | None:
    """Suggested factor for a block of ``real_size_mm`` (``None`` for
    missing or zero bounds). Below :data:`ONE_TO_ONE_FROM_MM` fit the longest
    side to :data:`FIT_MM`, rounded down to 1-2-5; from there to
    :data:`BED_MM` print 1:1; beyond the bed fit :data:`FIT_MM` the same way.
    The floor always wins: the result is never below ``min_scale`` (it may
    then be a non-1-2-5 number, and its printed size may exceed 100 mm)."""
    longest = max(real_size_mm) if real_size_mm else 0.0
    if not math.isfinite(longest) or longest <= 0:
        return None
    if ONE_TO_ONE_FROM_MM <= longest <= BED_MM:
        suggested = 1.0
    else:
        suggested = _round_down_125(FIT_MM / longest)
    if min_scale is not None and suggested < min_scale:
        suggested = min_scale
    return suggested


def real_size_mm(printed: PrintedSolid) -> list[float]:
    """Unscaled bounding-box extents (x, y, z) in millimetres of the printed
    solid in its block frame."""
    lo, hi = cad_bulk.expr_aabb(printed.design, printed.design.whole())
    return [float(v) * 1000.0 for v in np.asarray(hi) - np.asarray(lo)]


@dataclass(frozen=True)
class PrintDescriptor:
    """The ``.json`` payload of ``/se/{slug}/print/{block}.json``."""

    real_size_mm: list[float]
    suggested_scale: float | None
    min_scale: float | None
    models: list[str]
    selected_model: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "real_size_mm": self.real_size_mm,
            "suggested_scale": self.suggested_scale,
            "suggested_scale_text": (
                None
                if self.suggested_scale is None
                else format_scale_factor(self.suggested_scale)
            ),
            "min_scale": self.min_scale,
            "models": self.models,
            "selected_model": self.selected_model,
        }


def descriptor(
    tree: SeTree, block: str, printed: PrintedSolid, model: str | None
) -> PrintDescriptor:
    """The dialog descriptor of a printable solid block. ``models`` lists
    only implemented model ids — none until printable-atomic-models."""
    size = real_size_mm(printed)
    floor = floor_for(tree, block, model)
    return PrintDescriptor(
        real_size_mm=size,
        suggested_scale=suggest_scale(size, floor),
        min_scale=floor,
        models=[],
        selected_model=None,
    )


def scaled_filename(design: str, block: str, scale: float, fmt: str) -> str:
    """``<design>-<block>-x<factor>.<fmt>``; scale 1 keeps the historical
    ``<design>-<block>-print.<fmt>``."""
    if scale == 1.0:
        return f"{design}-{block}-print.{fmt}"
    return f"{design}-{block}-x{format_scale_factor(scale)}.{fmt}"
