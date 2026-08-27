"""Footprint resolution (Flow B) — lazy, per-selected-part.

Footprints are the catalog's expensive half: pad geometry + the pin-name->pad
map, fetched from EasyEDA (:mod:`precis.pcb.easyeda`) and parsed ourselves —
no parametric generation (user decision 2026-08-27): footprints are pulled,
never synthesized. Every JLCPCB-assemblable part has one by construction
(JLC assembly places from it), so **no EasyEDA footprint means the part is
not selectable** — there is no fallback generator to fall back to. We do NOT
fetch all ~300k catalog parts — only the few a design actually selects — and
cache the result in ``part_footprints`` (keyed by C-number, FK-free so the
Flow-A catalog swap never touches it).

The fetch is pluggable: ``ensure_footprint(store, lcsc, fetcher=...)`` returns
the cache row, fetching+caching on a miss. The default fetcher hits EasyEDA
live; tests inject a fake fetcher instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: A fetcher takes a C-number and returns the footprint dict
#: ``{pads, pin_map, courtyard, centroid, source, raw}`` or None.
Fetcher = Callable[[str], "dict[str, Any] | None"]


def ensure_footprint(
    store: Store, lcsc: str, *, fetcher: Fetcher | None = None
) -> dict[str, Any] | None:
    """Return the cached footprint for ``lcsc``, fetching + caching on a miss.

    ``store`` provides ``part_footprint_get`` / ``part_footprint_put``.
    Returns None if the part has no resolvable footprint.
    """
    lcsc = lcsc.strip().upper()
    cached = store.part_footprint_get(lcsc)
    if cached is not None:
        return cached
    fetch = fetcher or _easyeda_fetch
    data = fetch(lcsc)
    if data is None:
        return None
    store.part_footprint_put(lcsc, data)
    return store.part_footprint_get(lcsc)


def _easyeda_fetch(lcsc: str) -> dict[str, Any] | None:  # pragma: no cover
    """Default fetcher: EasyEDA over the network, parsed into canonical
    pads/pin_map/courtyard/centroid. Exercised by the worker/CLI paths that
    have network; unit tests inject a fake fetcher instead of hitting it."""
    from precis.pcb.easyeda import fetch_component, parse_component

    doc = fetch_component(lcsc)
    if doc is None:
        return None
    return parse_component(doc)
