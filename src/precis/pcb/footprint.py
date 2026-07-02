"""Footprint resolution (ADR 0042 §5, Flow B) — lazy, per-selected-part.

Footprints are the catalog's expensive half: pad geometry + the pin-name→pad
map, converted from LCSC via **easyeda2kicad**. We do NOT convert all ~300k
parts — only the few a design actually selects — and cache the result in
``part_footprints`` (keyed by C-number, FK-free so the Flow-A catalog swap
never touches it).

The fetch is pluggable: ``ensure_footprint(store, lcsc, fetcher=...)`` returns
the cache row, fetching+caching on a miss. The default fetcher is
easyeda2kicad (an optional, network-bound dependency — gated like the cad
exporters); tests inject a fake fetcher. (Phase 2 adds the internal IPC-7351
land-pattern generator for standard packages — ADR 0042 §5 footprint tiers.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis.errors import Unsupported

#: A fetcher takes a C-number and returns the footprint dict
#: ``{pads, pin_map, courtyard, centroid, kicad_mod, source}`` or None.
Fetcher = Callable[[str], "dict[str, Any] | None"]


def ensure_footprint(
    store: Any, lcsc: str, *, fetcher: Fetcher | None = None
) -> dict[str, Any] | None:
    """Return the cached footprint for ``lcsc``, fetching + caching on a miss.

    ``store`` provides ``part_footprint_get`` / ``part_footprint_put``.
    Returns None if the part has no resolvable footprint.
    """
    lcsc = lcsc.strip().upper()
    cached = store.part_footprint_get(lcsc)
    if cached is not None:
        return cached
    fetch = fetcher or _easyeda2kicad_fetch
    data = fetch(lcsc)
    if data is None:
        return None
    store.part_footprint_put(lcsc, data)
    return store.part_footprint_get(lcsc)


def _easyeda2kicad_fetch(lcsc: str) -> dict[str, Any] | None:  # pragma: no cover
    """Real fetch via easyeda2kicad (optional + network). Gated like the cad
    exporters — a missing dep is an Unsupported with the install hint, not a
    crash. Full conversion (EasyEDA → pads/pin_map/courtyard) is wired in the
    deploy image where the dependency + network are present."""
    try:
        import easyeda2kicad  # noqa: F401
    except ImportError as exc:
        raise Unsupported(
            "footprint fetch needs the easyeda2kicad backend",
            next="install it:  pip install 'precis-mcp[pcb]'  (or pre-cache "
            "footprints via the parts_refresh worker)",
        ) from exc
    # The conversion (EasyEDA component → KiCad pads/pin_map/courtyard) lands
    # with the deploy wiring; until then a present dep still returns None so
    # callers degrade rather than guess geometry.
    return None
