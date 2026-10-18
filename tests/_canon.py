"""Platform-tolerant canonical form for pinned geometry digests.

GEOS derives round-join arc segment counts from trig, and macOS libm and
glibc disagree by a vertex or two (gr495146: 950 vs 948 per pad), so a hash
over raw vertex lists differs per platform. :func:`canon` replaces every
polygon vertex list with a vertex-count-invariant summary and rounds floats;
everything else stays exact.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _is_point(v: Any) -> bool:
    return (
        isinstance(v, list | tuple)
        and len(v) == 2
        and all(isinstance(c, int | float) and not isinstance(c, bool) for c in v)
    )


def _is_ring(v: Any) -> bool:
    return isinstance(v, list | tuple) and len(v) >= 3 and all(_is_point(p) for p in v)


def _ring_stats(ring: Any) -> tuple[float, float, float, float, float, float, float]:
    """(signed-area-abs, cx, cy, minx, miny, maxx, maxy) by shoelace, no shapely."""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    a2 = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=True):
        cross = x0 * y1 - x1 * y0
        a2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if math.isclose(a2, 0.0, abs_tol=1e-12):
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    else:
        cx, cy = cx / (3 * a2), cy / (3 * a2)
    return abs(a2) / 2, cx, cy, min(xs), min(ys), max(xs), max(ys)


def _num(x: float, ndigits: int) -> float:
    r = round(x, ndigits)
    return 0.0 if r == 0 else r  # maps -0.0 to 0.0


def canon(obj: Any, ndigits: int = 4) -> Any:
    """JSON-ready form of ``obj``: polygons summarised, floats rounded.

    A polygon is a list of three or more ``[x, y]`` points, or a list of
    such rings. It becomes ``{area, bounds, centroid, n_rings}`` (first ring
    only for area/bounds/centroid; holes count in ``n_rings``).
    """
    if isinstance(obj, float):
        return _num(obj, ndigits)
    if isinstance(obj, dict):
        return {str(k): canon(v, ndigits) for k, v in obj.items()}
    if _is_ring(obj):
        return _polygon([obj], ndigits)
    if isinstance(obj, list | tuple) and obj and all(_is_ring(r) for r in obj):
        return _polygon(list(obj), ndigits)
    if isinstance(obj, list | tuple):
        return [canon(v, ndigits) for v in obj]
    return obj


def _polygon(rings: list[Any], ndigits: int) -> dict[str, Any]:
    area, cx, cy, x0, y0, x1, y1 = _ring_stats(rings[0])
    return {
        "area": _num(area, ndigits),
        "bounds": [_num(b, ndigits) for b in (x0, y0, x1, y1)],
        "centroid": [_num(cx, ndigits), _num(cy, ndigits)],
        "n_rings": len(rings),
    }


def digest(obj: Any, ndigits: int = 4) -> str:
    """16-hex sha256 of the canonical JSON of ``obj``."""
    blob = json.dumps(canon(obj, ndigits), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def assert_close(a: Any, b: Any, atol: float = 1e-4) -> None:
    """Assert two canonical-or-raw structures match, floats within ``atol``.

    Structure, keys, ints, strings and bools must be equal; only floats get
    the tolerance (after :func:`canon` with a fine ``ndigits``).
    """
    ca, cb = canon(a, 9), canon(b, 9)
    _close(ca, cb, atol, "$")


def _close(a: Any, b: Any, atol: float, path: str) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), f"{path}: keys {sorted(a)} != {sorted(b)}"
        for k in a:
            _close(a[k], b[k], atol, f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _close(x, y, atol, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert math.isclose(a, b, rel_tol=0.0, abs_tol=atol), f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"
