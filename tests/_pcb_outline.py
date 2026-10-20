"""Shared test helper: a closed board outline.

A board without a closed outline is invalid (Reto 2026-10-20), so every
test that routes or DRC-checks a board gives it one."""

from __future__ import annotations

from typing import Any


def closed_outline(
    x0: float = -50.0, y0: float = -50.0, x1: float = 150.0, y1: float = 150.0
) -> dict[str, Any]:
    """A rectangle ``ftype='outline'`` feature. The default is large enough
    to enclose any test board's parts with margin."""
    return {
        "ftype": "outline",
        "geom": {"path": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]},
    }
