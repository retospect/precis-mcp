"""Surface-Pourbaix resting-state map — the slice-2 read over the stored
``meta.che_sweep`` this package's ``surface_coverage_scan`` job writes.

**Import path, not implementation.** The builder lives in
:mod:`precis.quest.pourbaix_map` (core) because the quest and structure
``view='pourbaix'`` surfaces are served by core handlers, and core may not
import a plugin (``tests/test_plugin_import_boundary.py`` — the same
boundary that put ``precis.utils.reaction_graph`` in core). This module is
the stable pathway-side name; pathway code and ``precis_web`` (outside the
boundary) may import here. Everything is pure reads over stored meta — no
engine call.
"""

from __future__ import annotations

from precis.quest.pourbaix_map import (
    BULK_LAYER_NOTE,
    DEFAULT_PH_WINDOW,
    SCAN_JOB_TYPE,
    build_map,
    bulk_layer,
    bulk_verdict,
    maps_for_structure,
    quest_maps,
    render_map,
    render_quest_maps,
    render_structure_maps,
    scan_jobs,
)

__all__ = [
    "BULK_LAYER_NOTE",
    "DEFAULT_PH_WINDOW",
    "SCAN_JOB_TYPE",
    "build_map",
    "bulk_layer",
    "bulk_verdict",
    "maps_for_structure",
    "quest_maps",
    "render_map",
    "render_quest_maps",
    "render_structure_maps",
    "scan_jobs",
]
