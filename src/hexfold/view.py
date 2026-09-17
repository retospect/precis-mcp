"""``hexfold view``: 3D ball-and-stick rendering of a .hx file.

matplotlib is an optional dependency (``hexfold[view]``); it is imported
lazily so the core package stays NumPy-only.  Rendering is deterministic:
the default camera looks down the principal axis of the stick coordinates,
then tilts by fixed 30 deg elevation / 20 deg azimuth offsets.
"""

from __future__ import annotations

import hashlib

import numpy as np

from .build import Net

# C grey, H near-white; anything else hashed to a stable mid-tone colour.
_ELEMENT_COLOURS = {"C": "#555555", "H": "#eeeeee"}


def _element_colour(element: str) -> str:
    if element in _ELEMENT_COLOURS:
        return _ELEMENT_COLOURS[element]
    h = hashlib.sha1(element.encode()).hexdigest()
    return "#" + h[:6]


def render(net: Net, coords: np.ndarray, out: str | None, show_h: bool) -> None:
    """Draw ``net`` at ``coords``; write PNG to ``out`` or show a window."""
    import matplotlib

    if out is not None:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    keep = [a.ord for a in net.atoms if show_h or a.element in net.lattice.elements]
    keep_set = set(keep)
    pos = coords[keep]

    fig = plt.figure(figsize=(8, 8))
    ax = fig.add_subplot(111, projection="3d")

    # bonds as line segments, clipped to kept atoms
    for i, j, _o in net.bonds:
        if i in keep_set and j in keep_set:
            ax.plot(
                [coords[i, 0], coords[j, 0]],
                [coords[i, 1], coords[j, 1]],
                [coords[i, 2], coords[j, 2]],
                color="#888888",
                linewidth=1.2,
                zorder=1,
            )

    # atoms coloured by element; termination atoms drawn smaller
    for a in net.atoms:
        if a.ord not in keep_set:
            continue
        c = coords[a.ord]
        small = a.element not in net.lattice.elements
        ax.scatter(
            c[0],
            c[1],
            c[2],
            s=18 if small else 60,
            color=_element_colour(a.element),
            edgecolors="#333333",
            linewidths=0.4,
            zorder=3,
        )

    # shade non-hexagon rings: pentagons warm, heptagons+ cool
    ring_col = {5: "#d95f02", 7: "#7570b3", 8: "#7570b3", 9: "#7570b3"}
    ord_pos = {a.ord: coords[a.ord] for a in net.atoms}
    from mpl_toolkits.mplot3d.art3d import (  # type: ignore[import-untyped]
        Poly3DCollection,
    )

    for ring in net.rings:
        col = ring_col.get(len(ring))
        if col is None:
            continue
        pts = np.array([ord_pos[o] for o in ring])
        ax.add_collection3d(
            Poly3DCollection([pts], facecolor=col, alpha=0.18, edgecolor="none"),
            zdir="z",
        )

    # equal aspect + deterministic camera: principal axis then fixed tilt
    span = pos.max(axis=0) - pos.min(axis=0)
    mid = pos.mean(axis=0)
    r = float(span.max()) / 2.0 or 1.0
    ax.set_xlim(mid[0] - r, mid[0] + r)
    ax.set_ylim(mid[1] - r, mid[1] + r)
    ax.set_zlim(mid[2] - r, mid[2] + r)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=30.0, azim=20.0)
    ax.set_axis_off()
    fig.tight_layout()

    if out is not None:
        fig.savefig(out, dpi=160, bbox_inches="tight")
        plt.close(fig)
    else:
        plt.show()
