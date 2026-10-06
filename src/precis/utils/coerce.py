"""Best-effort scalar coercion helpers shared across parsers and handlers.

Each helper answers "what number is this untrusted value?" without raising;
they differ only in what they reject, so pick by the failure mode wanted:

* :func:`as_float` / :func:`to_int` -- ``float()`` / ``int()`` or ``None``.
* :func:`num` -- a real ``int``/``float`` (bools and strings rejected) or ``None``.
* :func:`num_finite` -- like :func:`num`, also ``None`` for NaN/inf.
* :func:`finite_or` -- ``float()`` of a value, ``default`` for ``None``/NaN/inf.
* :func:`float_or_zero` -- ``float()`` of a value, ``0.0`` for empty/unparseable.

Pure -- no DB, no IO.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "as_float",
    "finite_or",
    "float_or_zero",
    "num",
    "num_finite",
    "to_int",
]


def as_float(v: Any) -> float | None:
    """``float(v)``, or ``None`` when it is not float-convertible."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    """``int(v)``, or ``None`` when it is not int-convertible."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def num(raw: Any) -> float | None:
    """``raw`` as a float if it is a real number (not a bool/str), else ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def num_finite(v: Any) -> float | None:
    """Like :func:`num` but NaN and +/-inf also give ``None``."""
    f = num(v)
    return f if f is not None and math.isfinite(f) else None


def finite_or(x: Any, default: float = 0.0) -> float:
    """``float(x)``; ``default`` for ``None`` or a non-finite value."""
    if x is None:
        return default
    v = float(x)
    return v if math.isfinite(v) else default


def float_or_zero(raw: Any) -> float:
    """``float(raw)``; ``0.0`` for ``None``, ``""`` or anything unparseable."""
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
