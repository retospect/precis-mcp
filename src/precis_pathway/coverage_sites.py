"""Requested-coverage → representable-theta normalisation — the pure half.

``surface_coverage_scan`` needs the engine's coverage snap in two places
that must never drift apart (:func:`runner.coverage_anchor_key`, which
pools scans by it, and :func:`coverage_job.with_defaults`, which fills the
default anchor set), so the arithmetic lives here the way
:mod:`precis_pathway.step_retry` holds its pure policy: no engine or store
imports, importable and testable on a host with neither.

What is being mirrored (``autocatpath.coverage.coverage_counts``): a
requested theta maps to an integer adsorbate count on the built
supercell — ``max(1, round(theta * n_top))`` capped at
``min(n_top, n_sites)`` — and the scan reports the ACHIEVED
``n / n_top``. On the (111) slab ``surface_coverage_scan`` is scoped to,
every usable site class exposes at least one site per surface atom, so
the cap is ``n_top``; and ``n_top`` — the count of ontop sites — is the
top layer's atom count, ``size[0] * size[1]`` (``SlabConfig``'s
(3, 3, 4) default → 9). ``n_top`` for a general miller cut can exceed
``size[0] * size[1]`` (e.g. a (110) cell has two atoms per plane); the
job type refuses non-(111) configs before this is ever asked of one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: ``autocatpath.config.SlabConfig.size``'s default — mirrored so a config
#: that leaves ``slab.size`` unset still snaps to the 3x3 the engine would
#: actually build. One constant, one mirror site.
_DEFAULT_SLAB_SIZE: tuple[int, int, int] = (3, 3, 4)


def slab_top_sites(config: Mapping[str, Any]) -> int:
    """``n_top`` for the slab the engine would build from ``config``
    (a raw config dict or a ``Config.to_dict()`` — ``slab.size`` is the
    only field read). ``0`` when no site count can be derived; callers
    leave such input un-snapped for the engine to judge."""
    size = (config.get("slab") or {}).get("size") or _DEFAULT_SLAB_SIZE
    try:
        return max(0, int(size[0]) * int(size[1]))
    except (TypeError, ValueError, IndexError):
        return 0


def representable_coverages(coverages: Any, n_top: int) -> Any:
    """The theta values the engine will actually run for ``coverages`` on an
    ``n_top``-site slab: each request snapped to ``n / n_top`` with
    ``n = min(n_top, max(1, round(theta * n_top)))`` (Python's own
    round-half-even ``round``, the same call the engine makes), duplicates
    collapsed and the set sorted so order and request granularity stop
    mattering — ``[0.25, 0.5, 0.75, 1.0]`` and ``[2/9, 4/9, 7/9, 1]`` on a
    9-site cell are the same anchor set. ``"all"`` expands to every count
    up to 1 ML. Input that is not a list of numbers, or ``n_top < 1``,
    passes through unchanged — the engine's own validation names the
    problem rather than this mirror masking it."""
    if n_top < 1:
        return coverages
    if isinstance(coverages, str):
        if coverages != "all":
            return coverages
        return [n / n_top for n in range(1, n_top + 1)]
    if not isinstance(coverages, list | tuple):
        return coverages
    try:
        counts = {min(n_top, max(1, round(float(t) * n_top))) for t in coverages}
    except (TypeError, ValueError):
        return coverages
    return [n / n_top for n in sorted(counts)]


__all__ = ["representable_coverages", "slab_top_sites"]
