"""The shared courtyard figure (gr515107): parts drawn as their real
courtyard outlines — bottom-side orange, top blue, overlapping parts
filled red — plus the board outline and a caller-supplied underlay.

``runstore.placement_svg`` (the run file ``view='topo'`` serves) and
the pcb-loop's ``placement.svg`` render through this one function so
both views agree on what a part looks like: a labelled courtyard
polygon, not a dot. Hand-rendered (not ``precis.pcb.svg``) so the run
file carries exactly its own layers and stays byte-deterministic —
equal inputs give identical output, the same contract the rest of the
run payload keeps.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Any

import numpy as np

_TOP = "#1f77b4"
_BOTTOM = "#ff7f0e"
_OVERLAP = "#d62728"


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def _ring_array(ring: Any) -> np.ndarray | None:
    """A ring as an ``(K, 2)`` array, or ``None`` when the part has no
    usable polygon (ringless synthetic designs)."""
    if ring is None:
        return None
    a = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    return a if len(a) >= 3 else None


def courtyard_figure(
    rings: Sequence[Any],
    *,
    refdes: Sequence[str],
    bottom: Sequence[bool],
    overlap: Collection[str] = frozenset(),
    outline: Any = None,
    centers: Any = None,
    underlay: Callable[[Callable[[float], str]], Iterable[str]] | None = None,
) -> str:
    """One courtyard-view SVG document.

    ``rings[i]`` is part ``refdes[i]``'s world-frame courtyard ring
    (closed or open — the closing point is tolerated, not required);
    ``bottom[i]`` its mounted side; ``overlap`` the refdes set filled
    red. ``outline`` is the board outline ring (drawn first, unfilled).
    ``centers`` ``(N, 2)`` fallback positions: a ringless part still
    renders as a coloured dot with its label. ``underlay`` is called
    with the coordinate formatter and returns extra svg fragments
    inserted between the outline and the parts — the topo run passes
    its airwire heatmap + crossing marks through it.
    """
    n = len(refdes)
    c = (
        np.asarray(centers, dtype=np.float64).reshape(-1, 2)
        if centers is not None
        else None
    )
    ring_pts = [_ring_array(rings[i] if i < len(rings) else None) for i in range(n)]
    out_arr = _ring_array(outline)

    stacks = [a for a in ring_pts if a is not None]
    if out_arr is not None:
        stacks.append(out_arr)
    if c is not None:
        stacks.append(c[np.isfinite(c).all(axis=1)])
    stacks = [a for a in stacks if len(a)]
    if stacks:
        allp = np.concatenate(stacks)
        lo, hi = allp.min(axis=0) - 2.0, allp.max(axis=0) + 2.0
    else:
        lo, hi = np.zeros(2), np.full(2, 10.0)

    body: list[str] = []
    if out_arr is not None:
        s = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in out_arr)
        body.append(
            f'<polygon points="{s}" fill="none" stroke="#000000" stroke-width="0.3"/>'
        )
    if underlay is not None:
        body.extend(underlay(_fmt))
    for i, name in enumerate(refdes):
        a = ring_pts[i]
        color = _OVERLAP if str(name) in overlap else (_BOTTOM if bottom[i] else _TOP)
        if a is None:
            if c is None or not np.isfinite(c[i]).all():
                continue
            lx, ly = float(c[i][0]), float(c[i][1])
            body.append(
                f'<circle cx="{_fmt(lx)}" cy="{_fmt(ly)}" r="0.7" fill="{color}"/>'
            )
        else:
            s = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in a)
            body.append(
                f'<polygon points="{s}" fill="{color}" fill-opacity="0.3" '
                f'stroke="{color}" stroke-width="0.1"/>'
            )
            open_pts = a[:-1] if len(a) > 1 and np.allclose(a[0], a[-1]) else a
            lx, ly = (float(v) for v in open_pts.mean(axis=0))
        body.append(
            f'<text x="{_fmt(lx)}" y="{_fmt(ly)}" font-size="1.6" '
            f'font-family="sans-serif" text-anchor="middle">{name}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_fmt(lo[0])} '
        f'{_fmt(lo[1])} {_fmt(hi[0] - lo[0])} {_fmt(hi[1] - lo[1])}">'
        + "".join(body)
        + "</svg>\n"
    )


__all__ = ["courtyard_figure"]
