"""The typed ``hexfold_scene`` entries are one registry
(:mod:`precis_se.atomic.generators.typed_features`), each module's
declared ``FEATURE_TYPE``/``FEATURE_TYPES`` is what dispatch reads, and
every registered type is documented in skill ``precis-hexfold-seams-help``
— the one place the list lives."""

from __future__ import annotations

import importlib
from pathlib import Path

from precis_se.atomic.generators import typed_features
from precis_se.atomic.generators.fin import build_fin
from precis_se.atomic.generators.sp3_seam import build_sp3_seam
from precis_se.atomic.generators.y_junction import build_y_junction

_SEAMS_HELP = (
    Path(__file__).resolve().parents[1]
    / "src/precis/data/skills/precis-hexfold-seams-help.md"
)


def test_registry_is_the_modules_declarations() -> None:
    declared: set[str] = set()
    for module_name, _func in typed_features._BUILDERS:
        declared |= set(
            typed_features._declared_types(importlib.import_module(module_name))
        )
    assert set(typed_features.typed_features()) == declared
    assert len(declared) >= 9  # the seam family as of 2026-10-15


def test_every_registered_type_has_a_seams_help_section() -> None:
    text = _SEAMS_HELP.read_text(encoding="utf-8")
    sections = [
        chunk for chunk in text.split("\n## ")[1:] if not chunk.startswith("See also")
    ]
    missing = [
        t
        for t in typed_features.typed_features()
        if not any(f"`{t}`" in s or f'"{t}"' in s for s in sections)
    ]
    assert not missing, f"no precis-hexfold-seams-help section names {missing}"


def test_dispatch_matches_the_old_chain() -> None:
    """No behaviour change: a single registered type goes to its builder;
    an unknown type, or several typed features, go to the Y adapter, which
    owns those refusals."""
    assert typed_features.builder_for(["fin-sp3-z"]) is build_fin
    assert typed_features.builder_for(["k3-sp3-a"]) is build_sp3_seam
    assert typed_features.builder_for(["k3-sp2-120-z"]) is build_y_junction
    assert typed_features.builder_for(["no-such-type"]) is build_y_junction
    assert typed_features.builder_for(["fin-sp3-z", "fin-sp3-z"]) is build_y_junction
