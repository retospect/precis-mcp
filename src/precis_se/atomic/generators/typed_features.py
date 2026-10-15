"""The exclusive typed ``hexfold_scene`` feature entries — one registry.

A ``hexfold_scene`` call whose single feature carries a ``"type"`` routes the
whole call to that type's builder instead of the authored-surface tiler.
Each builder module declares the type(s) it owns as ``FEATURE_TYPE`` (or
``FEATURE_TYPES`` for a family); :data:`_BUILDERS` lists the modules, and
:func:`typed_features` reads the declarations, so a new entry is one line
here plus its module's constant — no hand-kept ``if typed == [...]`` chain.
Every registered type has a section in skill ``precis-hexfold-seams-help``
(pinned by ``tests/test_hexfold_typed_features.py``).

The straight Y (:mod:`.y_junction`) also owns every typed declaration the
registry does not match — mixed or several typed features, unknown types —
because its adapter carries those refusals.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from functools import cache
from typing import Any

from precis_se.atomic.generators._types import GeneratedBlock

Builder = Callable[[dict[str, Any]], GeneratedBlock]

#: (module, builder function) per typed entry family. Imported lazily: the
#: builders pull in hexfold's solvers, which a plain scene build never needs.
_BUILDERS: tuple[tuple[str, str], ...] = (
    ("precis_se.atomic.generators.y_junction", "build_y_junction"),
    ("precis_se.atomic.generators.fin", "build_fin"),
    ("precis_se.atomic.generators.fin120", "build_fin120"),
    ("precis_se.atomic.generators.rail120", "build_rail120"),
    ("precis_se.atomic.generators.channel", "build_channel"),
    ("precis_se.atomic.generators.tunnel", "build_tunnel_reservoirs"),
    ("precis_se.atomic.generators.sp3_seam", "build_sp3_seam"),
)

#: The refusal owner for typed declarations the registry does not match.
_FALLBACK = ("precis_se.atomic.generators.y_junction", "build_y_junction")


def _declared_types(module: Any) -> tuple[str, ...]:
    if hasattr(module, "FEATURE_TYPES"):
        return tuple(module.FEATURE_TYPES)
    return (str(module.FEATURE_TYPE),)


@cache
def typed_features() -> dict[str, Builder]:
    """``{feature type: builder}`` for every registered entry."""
    out: dict[str, Builder] = {}
    for module_name, func_name in _BUILDERS:
        module = importlib.import_module(module_name)
        builder = getattr(module, func_name)
        for feature_type in _declared_types(module):
            if feature_type in out:
                raise RuntimeError(f"typed feature {feature_type!r} registered twice")
            out[feature_type] = builder
    return out


def builder_for(typed: list[Any]) -> Builder:
    """The builder for a call's typed feature list: the registered entry
    when exactly one feature is typed with a registered type, else the
    straight Y adapter, which refuses what it cannot build."""
    if len(typed) == 1 and isinstance(typed[0], str):
        builder = typed_features().get(typed[0])
        if builder is not None:
            return builder
    module_name, func_name = _FALLBACK
    fallback: Builder = getattr(importlib.import_module(module_name), func_name)
    return fallback
