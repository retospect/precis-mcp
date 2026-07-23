"""Server runtime — public surface.

``runtime.py`` used to be one 2400-line module; it's now a package split
by concern (``dispatch`` / ``search`` / ``angle`` / ``hints`` / ``error``,
plus ``core`` for the ``PrecisRuntime`` class itself and ``factory`` for
``build_runtime``). This module re-exports the same names every existing
``from precis.runtime import X`` call site (in this repo and any caller
outside it) already relies on — the split is invisible from here down.
"""

from __future__ import annotations

from precis.config import PrecisConfig
from precis.runtime.core import PrecisRuntime
from precis.runtime.factory import _connect_store_or_raise, build_runtime

__all__ = [
    "PrecisConfig",
    "PrecisRuntime",
    "_connect_store_or_raise",
    "build_runtime",
]
