"""Bond-length, bond-angle and POAV1 pyramidalization statistics for a scene.

Pure numpy over the scene's cartesian coordinates and bond list (Å, degrees);
backs ``view='stats'`` and the one-line stats in the structure TOC head. The
geometry *tier* of the coordinates (stick preview vs relaxed) is the caller's
to state — this module only measures.

POAV1 (Haddon): for a 3-coordinated atom with unit bond vectors ``U`` (rows),
solve ``U v = 1``, normalise ``v``; θp = angle(v, bond) − 90° (equal for all
three bonds by construction). Planar sp2 → 0°, C60 → 11.6°, sp3 → 19.47°.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from . import probe
from .scene import Scene

#: Printed beside the measured values.
REFERENCE = "sp2 C–C 1.42 Å, 120°; θp: graphene 0, C60 11.6, sp3 19.47 (°)"
THETA_C60 = 11.6
THETA_HIGH = 15.0


@dataclass
class Stat:
    """Summary of one sample set; ``min_at``/``max_at`` are atom-label tuples."""

    count: int
    mean: float
    std: float
    min: float
    max: float
    min_at: tuple[str, ...]
    max_at: tuple[str, ...]


@dataclass
class PoavStats:
    count: int
    mean: float
    p95: float
    max: float
    max_at: str
    above_c60: int
    above_15: int


@dataclass
class GeometryStats:
    bonds: dict[tuple[str, str], Stat] = field(default_factory=dict)
    angles: dict[tuple[str, str], Stat] = field(default_factory=dict)  # (centre, ring)
    poav: PoavStats | None = None


def _summ(samples: list[tuple[float, tuple[str, ...]]]) -> Stat:
    vals = np.array([s[0] for s in samples])
    lo, hi = int(vals.argmin()), int(vals.argmax())
    return Stat(
        count=len(samples),
        mean=float(vals.mean()),
        std=float(vals.std()),
        min=float(vals[lo]),
        max=float(vals[hi]),
        min_at=samples[lo][1],
        max_at=samples[hi][1],
    )


def _bond_vector(
    scene: Scene, i: str, j: str, image: tuple[int, int, int]
) -> np.ndarray:
    cell = scene.cell
    fi, fj = scene.atoms[i].frac, scene.atoms[j].frac
    if not any(image):
        _, image = cell.mic(fi, fj)
    return np.asarray(cell.frac_to_cart(fj + np.array(image) - fi), dtype=float)


def poav_theta(vecs: np.ndarray) -> float:
    """POAV1 θp (degrees) from three bond vectors (rows); NaN if degenerate.

    The ``U v = 1`` solution is the direction at equal angle to all three
    unit bond vectors, i.e. the normal of the plane through their tips; the
    normal form is used because ``U`` is singular exactly in the planar case
    (θp = 0). θp = |angle(v, bond) − 90°| = asin(|n̂·u|).
    """
    u = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    n = np.cross(u[1] - u[0], u[2] - u[0])
    norm = float(np.linalg.norm(n))
    if norm < 1e-12:
        return float("nan")
    return float(np.degrees(np.arcsin(min(1.0, abs(float((u @ n).mean()) / norm)))))


def _smallest_ring_sizes(scene: Scene, triples: list[tuple[str, str, str]]) -> dict:
    rings = probe.rings(scene, 8)  # smallest-first
    by_atom: dict[str, list[frozenset[str]]] = {}
    for r in rings:
        fs = frozenset(r)
        for a in r:
            by_atom.setdefault(a, []).append(fs)
    out = {}
    for t in triples:
        out[t] = next((len(r) for r in by_atom.get(t[1], []) if r.issuperset(t)), None)
    return out


def compute(scene: Scene) -> GeometryStats:
    """Measure every bond, every angle at each bonded centre, and POAV1 θp."""
    nbrs: dict[str, list[tuple[str, np.ndarray]]] = {a: [] for a in scene.atoms}
    blen: dict[tuple[str, str], list[tuple[float, tuple[str, ...]]]] = {}
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        v = _bond_vector(scene, b.i, b.j, b.image)
        nbrs[b.i].append((b.j, v))
        nbrs[b.j].append((b.i, -v))
        pair = tuple(sorted((scene.atoms[b.i].element, scene.atoms[b.j].element)))
        blen.setdefault((pair[0], pair[1]), []).append(
            (float(np.linalg.norm(v)), (b.i, b.j))
        )
    stats = GeometryStats(bonds={k: _summ(v) for k, v in blen.items()})

    raw: list[tuple[str, float, tuple[str, str, str]]] = []
    thetas: list[tuple[float, str]] = []
    for c, lst in nbrs.items():
        elem = scene.atoms[c].element
        for (a, va), (d, vb) in combinations(lst, 2):
            cosang = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb)))
            raw.append(
                (elem, float(np.degrees(np.arccos(np.clip(cosang, -1, 1)))), (a, c, d))
            )
        if len(lst) == 3:
            th = poav_theta(np.array([v for _, v in lst]))
            if not np.isnan(th):
                thetas.append((th, c))
    sizes = _smallest_ring_sizes(scene, [r[2] for r in raw]) if raw else {}
    groups: dict[tuple[str, str], list[tuple[float, tuple[str, ...]]]] = {}
    for elem, ang, trip in raw:
        n = sizes.get(trip)
        groups.setdefault((elem, f"{n}-ring" if n else "acyclic"), []).append(
            (ang, trip)
        )
    stats.angles = {k: _summ(v) for k, v in groups.items()}
    if thetas:
        arr = np.array([t[0] for t in thetas])
        stats.poav = PoavStats(
            count=len(arr),
            mean=float(arr.mean()),
            p95=float(np.percentile(arr, 95)),
            max=float(arr.max()),
            max_at=thetas[int(arr.argmax())][1],
            above_c60=int((arr > THETA_C60).sum()),
            above_15=int((arr > THETA_HIGH).sum()),
        )
    return stats


def dominant_pair(stats: GeometryStats) -> tuple[tuple[str, str], Stat] | None:
    if not stats.bonds:
        return None
    k = max(stats.bonds, key=lambda p: stats.bonds[p].count)
    return k, stats.bonds[k]


def _pair(p: tuple[str, str]) -> str:
    return f"{p[0]}–{p[1]}"


def bond_line(p: tuple[str, str], s: Stat) -> str:
    return f"{_pair(p)} {s.mean:.3f} ± {s.std:.3f} ({s.min:.3f}..{s.max:.3f}) Å"


def poav_line(p: PoavStats) -> str:
    return f"θp mean/p95/max {p.mean:.1f}/{p.p95:.1f}/{p.max:.1f}°"


def head_line(stats: GeometryStats) -> str | None:
    """The one-line summary for the TOC head, or None if there are no bonds."""
    dom = dominant_pair(stats)
    if dom is None:
        return None
    line = bond_line(*dom)
    if stats.poav:
        line += " · " + poav_line(stats.poav)
    return line


def render(stats: GeometryStats, *, tier: str) -> str:
    """Full ``view='stats'`` body. Probe the labelled atoms with view='atom'."""
    out = [f"# geometry stats · tier: {tier}", f"# reference: {REFERENCE}"]
    if not stats.bonds:
        out.append("no bonds — nothing to measure")
        return "\n".join(out)
    out.append("\n## bond lengths (Å)")
    out.append("pair | n | mean | std | min (atoms) | max (atoms)")
    for p, s in sorted(stats.bonds.items(), key=lambda kv: -kv[1].count):
        out.append(
            f"{_pair(p)} | {s.count} | {s.mean:.3f} | {s.std:.3f} | "
            f"{s.min:.3f} ({'-'.join(s.min_at)}) | {s.max:.3f} ({'-'.join(s.max_at)})"
        )
    out.append("\n## bond angles (°) per centre element / smallest ring")
    out.append("centre | ring | n | mean | std | min (atoms) | max (atoms)")
    for (elem, ring), s in sorted(
        stats.angles.items(), key=lambda kv: (kv[0][0], -kv[1].count)
    ):
        out.append(
            f"{elem} | {ring} | {s.count} | {s.mean:.2f} | {s.std:.2f} | "
            f"{s.min:.2f} ({'-'.join(s.min_at)}) | {s.max:.2f} ({'-'.join(s.max_at)})"
        )
    out.append("\n## POAV1 pyramidalization θp (3-coordinated atoms)")
    pv = stats.poav
    if pv is None:
        out.append("no 3-coordinated atoms")
    else:
        out.append(
            f"n {pv.count} · mean {pv.mean:.2f}° · p95 {pv.p95:.2f}° · "
            f"max {pv.max:.2f}° ({pv.max_at}) · "
            f">{THETA_C60}° (C60): {pv.above_c60} · >{THETA_HIGH:g}°: {pv.above_15}"
        )
    return "\n".join(out)
