"""``view='sites'`` — the valve's attachment-site enumerator, and the
shape-and-polarity complementarity stub that scores it (tools 3–4 of
``docs/backlog/rotary-ratchet-valve.md`` "Design tool set", hexfold spec
§28.8).

**Sites.** Every carbon that can take a substituent, classified three
ways:

- *ring taxonomy* — ``sp3`` when the carbon is four-or-more-coordinated
  on single bonds (a graft junction — no free valence, counted but not a
  site), ``substituted`` when it already carries a non-C non-H group,
  ``open-edge`` under three carbon neighbours (a rim dangling bond —
  the H cap, if any, is replaceable), else the smallest C–C ring it
  closes on the bond graph (5/6/7/8, ``non-ring`` past that);
- *face* — which side of the local surface the substituent would grow
  on: the free direction's sign against the block's envelope axis
  (``out`` = away from the axis, ``in`` = toward the bore, ``axial`` =
  along it, the rim dangling bond — axial only for a zigzag rim; an
  armchair rim's dangling bond has a radial component). A sp² wall
  carbon is a site on each free face — one row per (atom, face);
- *steric exposure* — the free fraction of a probe sphere of radius
  ``exposure_A`` (default 3.0 Å) centred one radius out along the free
  direction, sampled by a deterministic Fibonacci lattice — 1.0 is wide
  open.

Each row also carries ``(z, θ, r)`` about the block's own envelope axis
in :mod:`precis_se.atomic.clearance`'s conventions (z from the anchor on
the axis line, θ off a fixed world basis); a block with no axis-bearing
envelope still lists ring/face=``n/a``/exposure with the cylindrical
columns blank. The pair form ``args={'a','b'}`` keeps only sites that
face the *other* block within ``pocket_gap_A`` — the valve doc's "the
rim's sites travel, the shell's sites are fixed".

**Complementarity stub.** ``args={'a','b','lining':{...}}`` on
``view='clearance'``: a per-block list of ``{'site': <ordinal> |
'ring:<class>', 'group': 'OH'|'NH3+'|'COO-'|'CH3'|'H'}`` assignments
decorates site carbons with a polarity class (OH polar-donor, NH3+
cationic, COO- anionic, CH3 apolar, H and undecorated bare). Over the
same read-only sweep as the clearance table, each pocket is scored per
angle: a shape term (volume vs ``cargo_A3``) plus a polarity term —
COMPATIBLE decorated contact pairs (+1: any polar with any polar,
cationic–anionic, apolar–apolar, and polar–charged ion–dipole pairs)
minus unlike ones (−1: polar or charged against apolar, same-sign
charges); ``bare`` never counts. When the term's sign flips across the
sweep the verdict names the admitting and rejecting arcs — contiguous
runs of the SWEEP ANGLE, wrapping at 360°; otherwise it says "no
rectification". It is a stub BY DESIGN — no energetics; the ladder above
it is the se atomic-mode relax/charge work.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.structure import Scene as StructScene
from precis_se.atomic import clearance as se_atomic_clearance
from precis_se.atomic.clearance import AtomCloud, SweepAxis
from precis_se.ops import SeTree

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


def _block_not_found(tree: SeTree, name: str) -> str:
    """The ``no such block`` message with the live roster — the handler's
    own :func:`precis_se.handler._block_not_found` restated here so this
    module doesn't import the handler it is called from."""
    roster = ", ".join(sorted(tree.blocks)) or "(none)"
    return f"no such block: {name!r}. Blocks in this design: {roster}"


#: Carbon–carbon bond cutoff used only when a bound structure carries no
#: declared bonds at all (generated scenes always do).
_CC_DERIVE_BOND_A = 1.9

#: A bond under this order counts as single for the sp³ test — aromatic
#: (~1.33) and double bonds do not, so an H-capped aromatic carbon with
#: four neighbours stays a ring site rather than reading as sp³.
_SINGLE_BOND_ORDER = 1.2

#: Smallest-ring search depth on the C–C graph — covers 5–8 membered
#: rings; anything longer reads as ``non-ring``.
_RING_MAX_DEPTH = 4

#: Free-direction vs axis-radial cosine thresholds for the face
#: classification: beyond ±0.3 the site faces clearly in or out; a
#: dangling bond along the tube axis reads ``axial``.
_FACE_COS = 0.3

#: A probe point inside the exposure sphere counts as free when no atom
#: sits this close — a ~vdW contact distance, not the full probe radius.
_EXPOSURE_CLEAR_A = 1.0

#: Deterministic sampling of the exposure sphere: Fibonacci directions ×
#: radial shells — reproducible, no RNG.
_EXPOSURE_DIRS = 40
_EXPOSURE_SHELLS = 5

#: A site "faces" the other block when the cross-block nearest atom lies
#: inside this half-cone of the free direction (and the pair's gap is
#: under pocket_gap_A — the same contact scale the clearance view uses).
_FACING_COS = 0.5

#: Substituent-group → polarity class for the complementarity stub.
GROUP_POLARITY: dict[str, str] = {
    "OH": "polar-donor",
    "NH3+": "cationic",
    "COO-": "anionic",
    "CH3": "apolar",
    "H": "bare",
}

#: The polarity classes that behave polar — donor and acceptor both; any
#: two of them pair compatible (donor–donor, donor–acceptor,
#: acceptor–acceptor).
_POLAR = frozenset({"polar-donor", "polar-acceptor"})

#: Classes that ARE sites — a carbon can take a substituent; ``sp3`` and
#: ``substituted`` are counted in the summary but never listed.
_SITE_CLASSES = ("5-ring", "6-ring", "7-ring", "8-ring", "open-edge", "non-ring")
_EXCLUDED_CLASSES = ("sp3", "substituted")
_GROUP_NAMES = tuple(GROUP_POLARITY)


def _fib_dirs(n: int) -> NDArray[np.float64]:
    """``n`` Fibonacci-spiral unit directions — a deterministic, even
    sphere covering."""
    i = np.arange(n, dtype=float) + 0.5
    phi = np.pi * (1.0 + 5.0**0.5) * i
    z = 1.0 - 2.0 * i / n
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], axis=1)


def _probe_lattice() -> NDArray[np.float64]:
    """Unit-sphere interior lattice: Fibonacci directions × shell radii
    (cube-root spacing for even volume fill), reused by every exposure
    probe."""
    shells = (np.arange(_EXPOSURE_SHELLS, dtype=float) + 0.5) / _EXPOSURE_SHELLS
    radii = shells ** (1.0 / 3.0)
    return (
        np.repeat(_fib_dirs(_EXPOSURE_DIRS), _EXPOSURE_SHELLS, axis=0)
        * np.tile(radii, _EXPOSURE_DIRS)[:, None]
    )


_LATTICE = _probe_lattice()


def _bond_graph(
    scene: StructScene, labels: list[str], pts_A: NDArray[np.float64]
) -> tuple[list[list[int]], list[bool]]:
    """``(adjacency, unsaturated)`` over the cloud's atom indices —
    ALL-element declared scene bonds when present (hexfold scenes always
    carry them), a C–C distance derivation as the fallback.
    ``unsaturated[i]`` is set when any bond at ``i`` exceeds
    :data:`_SINGLE_BOND_ORDER` — the sp³ test needs it so an aromatic C–H
    (three aromatic C bonds + one H) doesn't read as four-coordinated."""
    index = {label: i for i, label in enumerate(labels)}
    adj: list[set[int]] = [set() for _ in labels]
    unsat = [False] * len(labels)
    n_bonds = 0
    for bond in scene.bonds:
        i, j = index.get(bond.i), index.get(bond.j)
        if i is None or j is None:
            continue
        adj[i].add(j)
        adj[j].add(i)
        if bond.order >= _SINGLE_BOND_ORDER:
            unsat[i] = unsat[j] = True
        n_bonds += 1
    if n_bonds == 0:
        from scipy.spatial import cKDTree

        carbon = [
            i for i, label in enumerate(labels) if scene.atoms[label].element == "C"
        ]
        pts = pts_A[carbon]
        for a, b in cKDTree(pts).query_pairs(_CC_DERIVE_BOND_A):
            adj[carbon[a]].add(carbon[b])
            adj[carbon[b]].add(carbon[a])
    return [sorted(s) for s in adj], unsat


def _hyb_of(scene: StructScene, cloud: AtomCloud) -> list[str | None]:
    """The generator-authored hybridization tag per cloud atom
    (``sp2``/``sp3`` straight from hexfold's net), or ``None``."""
    return [scene.atoms[label].hybridization for label in cloud.labels]


def _c_adjacency(adj: list[list[int]], elements: list[str]) -> list[list[int]]:
    """The C–C subgraph of a full bond graph — the ring-search graph."""
    return [[j for j in nbrs if elements[j] == "C"] for nbrs in adj]


def _min_ring_size(adj: list[list[int]], root: int) -> int | None:
    """Smallest ring through ``root`` on the C–C graph, or ``None`` —
    BFS to :data:`_RING_MAX_DEPTH`, tracking which root-neighbour subtree
    each visited node belongs to: an edge joining different subtrees
    closes a ring through root of ``depth[u] + depth[v] + 1``, and the
    smallest such length is the answer."""
    subtree: dict[int, int] = {}
    depth: dict[int, int] = {}
    queue: deque[int] = deque()
    for nb in adj[root]:
        if nb in subtree:
            return 3  # two root-neighbours bonded to each other
        subtree[nb] = nb
        depth[nb] = 1
        queue.append(nb)
    best: int | None = None
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v == root:
                continue
            if v not in subtree:
                subtree[v] = subtree[u]
                depth[v] = depth[u] + 1
                if depth[v] <= _RING_MAX_DEPTH:
                    queue.append(v)
            elif subtree[v] != subtree[u]:
                cand = depth[u] + depth[v] + 1
                best = cand if best is None else min(best, cand)
    return best


def ring_classes(
    cloud: AtomCloud,
    adj: list[list[int]],
    unsat: list[bool],
    hyb: list[str | None] | None = None,
) -> list[str | None]:
    """Per-atom ring taxonomy parallel to ``cloud`` — ``None`` for
    non-carbons (never site candidates), ``substituted`` for a carbon
    carrying a non-C non-H group, ``sp3`` for a four-or-more-coordinated
    all-single-bond carbon (or one the generator tagged ``sp3`` — a graft
    junction has no free valence), ``open-edge`` under three C
    neighbours, else the smallest C–C ring (5–8) or ``non-ring``."""
    cadj = _c_adjacency(adj, cloud.elements)
    out: list[str | None] = []
    for i, el in enumerate(cloud.elements):
        if el != "C":
            out.append(None)
            continue
        if any(cloud.elements[j] not in ("C", "H") for j in adj[i]):
            out.append("substituted")
            continue
        if (hyb is not None and hyb[i] == "sp3") or (len(adj[i]) >= 4 and not unsat[i]):
            out.append("sp3")
            continue
        if len(cadj[i]) < 3:
            out.append("open-edge")
            continue
        size = _min_ring_size(cadj, i)
        out.append(f"{size}-ring" if size in (5, 6, 7, 8) else "non-ring")
    return out


def _free_dirs(
    i: int,
    pts: NDArray[np.float64],
    cadj: list[list[int]],
    adj: list[list[int]],
) -> list[NDArray[np.float64]]:
    """The directions a substituent could grow from atom ``i`` — the
    dangling-bond direction for open-edge carbons (away from the C
    neighbours' centroid, the all-neighbour centroid for a carbon with
    no C neighbours at all), both sides of the neighbour plane's normal
    for a saturated sp² wall carbon."""
    p = pts[i]
    if len(cadj[i]) < 3:
        ref = cadj[i] or adj[i]
        if not ref:
            return []
        d = p - pts[ref].mean(axis=0)
        n = float(np.linalg.norm(d))
        return [d / n] if n > 1e-9 else []
    centred = pts[cadj[i]] - p
    _u, _s, vt = np.linalg.svd(centred)
    normal = vt[-1]
    return [normal, -normal]


def _exposure(
    atom_pos: NDArray[np.float64],
    free_dir: NDArray[np.float64],
    occluder_tree: Any,
    exposure_A: float,
) -> float:
    """Free fraction of the probe sphere centred ``exposure_A`` out along
    ``free_dir`` — the deterministic lattice's points whose nearest
    occluding atom is at least :data:`_EXPOSURE_CLEAR_A` away."""
    centre = atom_pos + free_dir * exposure_A
    dists, _ = occluder_tree.query(centre + _LATTICE * exposure_A)
    return float((dists >= _EXPOSURE_CLEAR_A).mean())


def _cyl(p: NDArray[np.float64], axis: SweepAxis) -> tuple[float, float, float]:
    """``(z, θ°, r)`` about the axis — z from the anchor along the axis,
    θ off the fixed world basis, r the axis distance."""
    v1, v2 = se_atomic_clearance._axis_basis(axis.dir_A)
    rel = p - axis.point_A
    z = float(np.dot(rel, axis.dir_A))
    theta = float(
        np.degrees(math.atan2(float(np.dot(rel, v2)), float(np.dot(rel, v1)))) % 360.0
    )
    r = float(np.linalg.norm(rel - z * axis.dir_A))
    return z, theta, r


def _face(free_dir: NDArray[np.float64], radial: NDArray[np.float64]) -> str:
    norm = float(np.linalg.norm(radial))
    if norm < 1e-9:
        return "axial"
    cos = float(np.dot(free_dir, radial / norm))
    if cos > _FACE_COS:
        return "out"
    if cos < -_FACE_COS:
        return "in"
    return "axial"


@dataclass
class Site:
    """One (atom, face) site row: the carbon's cloud ordinal and label,
    ring class, face vs the envelope axis (``n/a`` when the block has no
    axis-bearing envelope), free direction, exposure, and cylindrical
    coordinates (``None`` without an axis)."""

    block: str
    ordinal: int
    label: str
    ring_class: str
    face: str
    normal: NDArray[np.float64]
    exposure: float
    z_A: float | None
    theta_deg: float | None
    r_A: float | None


def enumerate_sites(
    cloud: AtomCloud,
    classes: list[str | None],
    adj: list[list[int]],
    axis: SweepAxis | None,
    exposure_A: float,
    occluder_pts: NDArray[np.float64] | None = None,
) -> list[Site]:
    """Every (carbon, free-face) site on ``cloud`` — only the real site
    classes (``sp3``/``substituted`` are excluded here and counted in the
    summary); the probe sphere sees the block's own atoms plus
    ``occluder_pts`` (the partner block, in the pair view). ``axis`` may
    be ``None`` — ring class, ``n/a`` face and exposure still render."""
    from scipy.spatial import cKDTree

    cadj = _c_adjacency(adj, cloud.elements)
    occl = (
        np.concatenate([cloud.pts_A, occluder_pts])
        if occluder_pts is not None and len(occluder_pts)
        else cloud.pts_A
    )
    occluder_tree = cKDTree(occl)
    sites: list[Site] = []
    for i, ring in enumerate(classes):
        if ring is None or ring in _EXCLUDED_CLASSES:
            continue
        if axis is not None:
            rel = cloud.pts_A[i] - axis.point_A
            radial = rel - float(np.dot(rel, axis.dir_A)) * axis.dir_A
        for free_dir in _free_dirs(i, cloud.pts_A, cadj, adj):
            face = _face(free_dir, radial) if axis is not None else "n/a"
            if face == "axial" and len(cadj[i]) >= 3:
                continue  # a wall normal that lands exactly on the axis —
                # vanishingly rare and meaningless as a face; skip it.
            exp = _exposure(cloud.pts_A[i], free_dir, occluder_tree, exposure_A)
            z, theta, r = (
                _cyl(cloud.pts_A[i], axis)
                if axis is not None
                else (
                    None,
                    None,
                    None,
                )
            )
            sites.append(
                Site(
                    block=cloud.name,
                    ordinal=i,
                    label=cloud.labels[i],
                    ring_class=ring,
                    face=face,
                    normal=free_dir,
                    exposure=exp,
                    z_A=z,
                    theta_deg=theta,
                    r_A=r,
                )
            )
    sites.sort(key=lambda s: (-s.exposure, s.block, s.ordinal, s.face))
    return sites


def _facing_sites(
    sites: list[Site],
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    pocket_gap_A: float,
) -> list[Site]:
    """The pair-view subset: sites whose free direction points at a
    partner-block atom within ``pocket_gap_A`` — the valve doc's
    travelling-rim half of the surface list."""
    field = se_atomic_clearance.gap_field(cloud_a, cloud_b)
    out: list[Site] = []
    for s in sites:
        if s.block == cloud_a.name:
            j = int(field.near_a[s.ordinal])
            gap = field.gaps_a[s.ordinal]
            partner = cloud_b.pts_A[j]
            atom = cloud_a.pts_A[s.ordinal]
        else:
            j = int(field.near_b[s.ordinal])
            gap = field.gaps_b[s.ordinal]
            partner = cloud_a.pts_A[j]
            atom = cloud_b.pts_A[s.ordinal]
        to = partner - atom
        n = float(np.linalg.norm(to))
        if n < 1e-9:
            continue
        if gap < pocket_gap_A and float(np.dot(s.normal, to / n)) > _FACING_COS:
            out.append(s)
    return out


def _vet_exposure_A(args: dict[str, Any]) -> float:
    raw = args.get("exposure_A", 3.0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"view='sites' 'exposure_A' must be a number (Å); got {raw!r}"
        ) from exc
    if not (0.5 <= value <= 20.0):
        raise BadInput(f"view='sites' 'exposure_A' must be in [0.5, 20] Å; got {value}")
    return value


def _vet_face(args: dict[str, Any]) -> str:
    face = str(args.get("face") or "both").strip().lower()
    if face not in ("in", "out", "both"):
        raise BadInput(
            f"view='sites' 'face' must be 'in', 'out' or 'both'; got {face!r}"
        )
    return face


def _fmt_sites(sites: list[Site], classes: dict[str, list[str | None]]) -> list[str]:
    """The site table plus the per-(class, face) summary — the summary
    covers the EXCLUDED classes too (``sp3``/``substituted`` count under
    a ``—`` face) so a graft junction is accounted for, not dropped."""
    lines: list[str] = []
    if not sites:
        lines.append("(no sites)")
    else:
        rows = [
            {
                "block": s.block,
                "ordinal": s.ordinal,
                "label": s.label,
                "ring": s.ring_class,
                "face": s.face,
                "exposure": f"{s.exposure:.2f}",
                "z (Å)": f"{s.z_A:.2f}" if s.z_A is not None else "—",
                "θ": f"{s.theta_deg:.1f}°" if s.theta_deg is not None else "—",
                "r (Å)": f"{s.r_A:.2f}" if s.r_A is not None else "—",
            }
            for s in sites
        ]
        lines.append(
            render_agent_table(
                rows,
                schema=[
                    "block",
                    "ordinal",
                    "label",
                    "ring",
                    "face",
                    "exposure",
                    "z (Å)",
                    "θ",
                    "r (Å)",
                ],
            )
        )
    counts: dict[tuple[str, str], int] = {}
    for s in sites:
        key = (s.ring_class, s.face)
        counts[key] = counts.get(key, 0) + 1
    for cls in classes.values():
        for ring in cls:
            if ring in _EXCLUDED_CLASSES:
                key = (str(ring), "—")
                counts[key] = counts.get(key, 0) + 1
    lines.append("")
    count_rows = [
        {"ring": ring, "face": face, "count": counts.get((ring, face), 0)}
        for ring in _SITE_CLASSES
        for face in ("in", "out", "axial", "n/a")
        if counts.get((ring, face), 0)
    ]
    count_rows += [
        {"ring": ring, "face": "—", "count": counts.get((ring, "—"), 0)}
        for ring in _EXCLUDED_CLASSES
        if counts.get((ring, "—"), 0)
    ]
    lines.append(render_agent_table(count_rows, schema=["ring", "face", "count"]))
    return lines


def _block_scene(store: Store, node: Any) -> StructScene:
    scene = se_atomic_clearance._load_scene(store, node.bound or "")
    if scene is None:
        raise BadInput(
            f"block {node.name!r} is bound to structure {node.bound!r}, "
            "which no longer resolves — the binding is dangling"
        )
    if not scene.atoms:
        raise BadInput(
            f"block {node.name!r}'s bound structure {node.bound!r} has "
            "no atoms — nothing to enumerate sites over"
        )
    return scene


def _require_atomic(
    store: Store, tree: SeTree, name: str
) -> tuple[Any, AtomCloud, StructScene]:
    node = tree.blocks.get(name)
    if node is None:
        raise NotFound(_block_not_found(tree, name))
    if not (node.bound_kind == "structure" and node.bound):
        raise BadInput(
            f"view='sites' needs structure-bound blocks; {name!r} is not "
            "bound — bind_structure or generate it first"
        )
    scene = _block_scene(store, node)
    return node, se_atomic_clearance.atom_cloud(node, scene), scene


def _sites_block(
    cloud: AtomCloud,
    scene: StructScene,
    axis: SweepAxis | None,
    exposure_A: float,
    occl: Any = None,
) -> tuple[list[Site], list[str | None]]:
    """``(sites, classes)`` — the bond graph is built once here and feeds
    the classifier AND the free-direction search (finding 5: no double
    walk of the scene's bonds)."""
    adj, unsat = _bond_graph(scene, cloud.labels, cloud.pts_A)
    classes = ring_classes(cloud, adj, unsat, _hyb_of(scene, cloud))
    return (
        enumerate_sites(cloud, classes, adj, axis, exposure_A, occluder_pts=occl),
        classes,
    )


def _axis_note(axis: SweepAxis | None) -> str:
    if axis is not None:
        return f"axis: {axis.note}"
    return (
        "axis: none — the block's envelope carries no axis; ring class, "
        "face n/a and exposure still report, z/θ/r stay blank"
    )


def render_sites(store: Store, tree: SeTree, args: dict[str, Any] | None) -> str:
    """``view='sites'`` — single-block form ``{'block': ...}`` or the
    pair form ``{'a','b'}`` (sites facing the partner within
    ``pocket_gap_A``)."""
    args = args or {}
    face = _vet_face(args)
    exposure_A = _vet_exposure_A(args)
    a_name = args.get("a")
    b_name = args.get("b")
    block_name = args.get("block")

    if block_name and (a_name or b_name):
        raise BadInput("view='sites' takes either 'block' or the pair {'a','b'}")
    if not block_name and not (a_name and b_name):
        raise BadInput(
            "view='sites' needs args={'block': <name>} for one block, or "
            "{'a','b'} for the pair view"
        )

    lines: list[str] = []
    if block_name:
        name = str(block_name).strip()
        node, cloud, scene = _require_atomic(store, tree, name)
        axis = se_atomic_clearance._envelope_axis(tree, node)
        sites, classes = _sites_block(cloud, scene, axis, exposure_A)
        if face != "both":
            sites = [s for s in sites if s.face == face]
        lines.append(
            f"# sites: {name!r} — {len(sites)} site row(s), "
            f"rung {se_atomic_clearance._rung_of(store, node)} — "
            "stick/preview geometry unless relaxed"
        )
        lines.append(_axis_note(axis))
        lines += _fmt_sites(sites, {name: classes})
        return "\n".join(lines) + "\n"

    a_name, b_name = str(a_name).strip(), str(b_name).strip()
    if a_name == b_name:
        raise BadInput("view='sites': 'a' and 'b' must differ")
    a_node, cloud_a, scene_a = _require_atomic(store, tree, a_name)
    b_node, cloud_b, scene_b = _require_atomic(store, tree, b_name)
    pocket_gap_A = se_atomic_clearance._pocket_gap_A(args)
    axis_a = se_atomic_clearance._envelope_axis(tree, a_node)
    axis_b = se_atomic_clearance._envelope_axis(tree, b_node)
    sites, classes_a = _sites_block(
        cloud_a, scene_a, axis_a, exposure_A, occl=cloud_b.pts_A
    )
    more, classes_b = _sites_block(
        cloud_b, scene_b, axis_b, exposure_A, occl=cloud_a.pts_A
    )
    sites += more
    facing = _facing_sites(sites, cloud_a, cloud_b, pocket_gap_A)
    if face != "both":
        facing = [s for s in facing if s.face == face]
    lines.append(
        f"# sites: {a_name!r} vs {b_name!r} — {len(facing)} facing site "
        f"row(s) within pocket_gap_A={pocket_gap_A:g} Å"
    )
    lines.append(
        f"rungs: {a_name}={se_atomic_clearance._rung_of(store, a_node)}, "
        f"{b_name}={se_atomic_clearance._rung_of(store, b_node)} — "
        "stick/preview geometry unless relaxed"
    )
    lines.append(
        "each (z, θ, r) is measured against the site's OWN block axis — "
        "see view='clearance' for the pair axis"
    )
    lines += _fmt_sites(facing, {a_name: classes_a, b_name: classes_b})
    return "\n".join(lines) + "\n"


# ── complementarity stub ──────────────────────────────────────────────


def _polarity_map(
    lining: Any,
    cloud_by_name: dict[str, AtomCloud],
    classes_by_name: dict[str, list[str | None]],
) -> dict[str, dict[int, str]]:
    """``lining`` → per-block ``{ordinal: polarity class}``; undecorated
    atoms are ``bare``. ``site`` is an atom ordinal or ``'ring:<class>'``
    (every carbon of that class — ``non-ring`` excluded: a carbon that
    closes no small ring isn't a nameable site class)."""
    if not isinstance(lining, dict) or not lining:
        raise BadInput(
            "clearance 'lining' must be an object like "
            "{'rotor': [{'site': 137 | 'ring:6-ring', 'group': 'OH'}]}"
        )
    unknown_blocks = sorted(set(lining) - set(cloud_by_name))
    if unknown_blocks:
        raise BadInput(
            f"lining names block(s) {unknown_blocks} outside the pair "
            f"{sorted(cloud_by_name)}"
        )
    selectable = tuple(c for c in _SITE_CLASSES if c != "non-ring")
    out: dict[str, dict[int, str]] = {name: {} for name in cloud_by_name}
    for name, entries in lining.items():
        if not isinstance(entries, list) or not entries:
            raise BadInput(
                f"lining[{name!r}] must be a non-empty list of "
                "{'site','group'} entries"
            )
        cloud, classes = cloud_by_name[name], classes_by_name[name]
        for entry in entries:
            if not isinstance(entry, dict):
                raise BadInput(f"lining[{name!r}] entries must be objects")
            unknown = sorted(set(entry) - {"site", "group"})
            if unknown:
                raise BadInput(
                    f"lining[{name!r}] unknown key(s) {unknown}; accepted: site, group"
                )
            group = str(entry.get("group", ""))
            if group not in GROUP_POLARITY:
                raise BadInput(
                    f"lining[{name!r}] group must be one of {_GROUP_NAMES}; "
                    f"got {group!r}"
                )
            site = entry.get("site")
            if site is None:
                raise BadInput(f"lining[{name!r}] entry lacks 'site'")
            if isinstance(site, str) and site.startswith("ring:"):
                ring = site[5:]
                if ring == "non-ring":
                    raise BadInput(
                        f"lining[{name!r}] site 'ring:non-ring' is not a "
                        "nameable site class — list ordinals instead"
                    )
                if ring not in _SITE_CLASSES:
                    raise BadInput(
                        f"lining[{name!r}] site 'ring:' class must be one of "
                        f"{selectable}; got {site!r}"
                    )
                for i, cls in enumerate(classes):
                    if cls == ring:
                        out[name][i] = GROUP_POLARITY[group]
                continue
            try:
                ordinal = int(site)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"lining[{name!r}] site must be an atom ordinal or "
                    f"'ring:<class>'; got {site!r}"
                ) from exc
            if not (0 <= ordinal < len(cloud.labels)):
                raise BadInput(
                    f"lining[{name!r}] site {ordinal} out of range "
                    f"(0..{len(cloud.labels) - 1})"
                )
            if cloud.elements[ordinal] != "C":
                raise BadInput(
                    f"lining[{name!r}] site {ordinal} is "
                    f"{cloud.elements[ordinal]}, not a carbon — sites are "
                    "carbons only"
                )
            out[name][ordinal] = GROUP_POLARITY[group]
    return out


def build_polarity(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    scene_a: StructScene,
    scene_b: StructScene,
    lining: Any,
) -> dict[str, dict[int, str]]:
    """Validate ``lining`` and return the per-block polarity map — runs
    BEFORE the sweep so a bad lining fails fast (one bond-graph build
    per block feeds the ring classes here and nothing else)."""
    classes_by_name: dict[str, list[str | None]] = {}
    for cloud, scene in (
        (cloud_a, scene_a),
        (cloud_b, scene_b),
    ):
        adj, unsat = _bond_graph(scene, cloud.labels, cloud.pts_A)
        classes_by_name[cloud.name] = ring_classes(
            cloud, adj, unsat, _hyb_of(scene, cloud)
        )
    return _polarity_map(
        lining,
        {cloud_a.name: cloud_a, cloud_b.name: cloud_b},
        classes_by_name,
    )


def vet_cargo_A3(args: dict[str, Any]) -> float:
    raw = args.get("cargo_A3", 100.0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"clearance 'cargo_A3' must be a number (Å³); got {raw!r}"
        ) from exc
    if value <= 0:
        raise BadInput(f"clearance 'cargo_A3' must be > 0 Å³; got {value}")
    return value


def _pair_score(pa: str, pb: str) -> int:
    """Compatibility of two decorated contact partners — +1 like, −1
    unlike, 0 whenever either side is bare.

    Like (the reviewer's ruling: "like" means COMPATIBLE): any polar
    class with any polar class (donor–donor, donor–acceptor,
    acceptor–acceptor), cationic–anionic, apolar–apolar, and
    polar–charged ion–dipole pairs. Unlike: polar or charged against
    apolar, and same-sign charges (cationic–cationic,
    anionic–anionic)."""
    if pa == "bare" or pb == "bare":
        return 0
    if pa == "apolar" or pb == "apolar":
        return 1 if pa == pb == "apolar" else -1
    if pa in _POLAR and pb in _POLAR:
        return 1
    if {pa, pb} == {"cationic", "anionic"}:
        return 1
    if pa in _POLAR or pb in _POLAR:
        return 1  # ion–dipole: polar next to a charge is favourable
    return -1  # cationic–cationic / anionic–anionic


def _pocket_contacts(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    atoms_a: list[int],
    atoms_b: list[int],
    threshold_A: float,
) -> list[tuple[int, int]]:
    """Cross-block contact pairs inside one pocket — every (a, b) atom
    pair whose vdW gap is under the pocket threshold."""
    if not atoms_a or not atoms_b:
        return []
    pa = cloud_a.pts_A[atoms_a]
    pb = cloud_b.pts_A[atoms_b]
    d = np.linalg.norm(pa[:, None, :] - pb[None, :, :], axis=2)
    g = d - cloud_a.radii_A[atoms_a][:, None] - cloud_b.radii_A[atoms_b][None, :]
    ii, jj = np.nonzero(g < threshold_A)
    return [(atoms_a[int(i)], atoms_b[int(j)]) for i, j in zip(ii, jj)]


def _score_pockets(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    pocket_list: list[Any],
    polarity: dict[str, dict[int, str]],
    threshold_A: float,
    cargo_A3: float,
) -> tuple[list[int], int]:
    """``(score per pocket, aggregate polarity term)`` — score = shape
    (pocket hull volume vs cargo) + polarity (compatible minus unlike
    decorated contact pairs)."""
    scores: list[int] = []
    term_all = 0
    for pocket in pocket_list:
        contacts = _pocket_contacts(
            cloud_a, cloud_b, pocket.atoms_a, pocket.atoms_b, threshold_A
        )
        term = 0
        for i, j in contacts:
            pa = polarity[cloud_a.name].get(i, "bare")
            pb = polarity[cloud_b.name].get(j, "bare")
            term += _pair_score(pa, pb)
        term_all += term
        shape = (
            1 if pocket.volume_A3 is not None and pocket.volume_A3 >= cargo_A3 else -1
        )
        scores.append(shape + term)
    return scores, term_all


def _arc_runs(
    terms: list[int],
    angles: list[float],
    wrap: bool,
) -> list[tuple[int, float, float]]:
    """Contiguous same-sign runs over the sweep, each ``(sign, first,
    last)`` — when ``wrap`` (a full 360° sweep) a run spanning the
    range end merges across the seam and its ``last`` exceeds 360
    (``300°–420°`` reads as one unbroken arc)."""
    runs: list[list[Any]] = []
    for angle, term in zip(angles, terms):
        sign = 1 if term > 0 else (-1 if term < 0 else 0)
        if runs and runs[-1][0] == sign:
            runs[-1][2] = angle
        else:
            runs.append([sign, angle, angle])
    if wrap and len(runs) > 1 and runs[0][0] == runs[-1][0]:
        first = runs.pop(0)
        runs[-1][2] = first[2] + 360.0
    return [(int(s), float(a), float(b)) for s, a, b in runs]


def render_complementarity(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    rows: list[Any],
    polarity: dict[str, dict[int, str]],
    cargo_A3: float,
    threshold_A: float,
    range_deg: float,
) -> list[str]:
    """The STUB scorer's section — the validated polarity map +
    per-angle best/worst pocket score + the arc verdict over SWEEP-ANGLE
    runs. ``rows`` are the sweep rows (each carrying its pockets)."""
    n_dec = {name: len(m) for name, m in polarity.items()}
    lines = [
        "\nlining (STUB — shape + polarity match, no energetics): "
        + ", ".join(f"{name} {n} decorated carbon(s)" for name, n in n_dec.items())
        + f"; cargo {cargo_A3:g} Å³",
        "polarity: OH=polar-donor, NH3+=cationic, COO-=anionic, CH3=apolar, "
        "undecorated=bare; compatible: polar–polar, cationic–anionic, "
        "apolar–apolar, polar–charged",
    ]
    table_rows = []
    angles: list[float] = []
    terms: list[int] = []
    for row in rows:
        scores, term = _score_pockets(
            cloud_a,
            row.cloud_b,
            row.pockets,
            polarity,
            threshold_A,
            cargo_A3,
        )
        angles.append(row.angle_deg)
        terms.append(term)
        table_rows.append(
            {
                "angle": f"{row.angle_deg:g}°",
                "best score": max(scores) if scores else "—",
                "worst score": min(scores) if scores else "—",
                "polarity term": term,
            }
        )
    lines.append(
        render_agent_table(
            table_rows, schema=["angle", "best score", "worst score", "polarity term"]
        )
    )
    runs = _arc_runs(terms, angles, wrap=abs(range_deg - 360.0) < 1e-9)
    pos = [r for r in runs if r[0] > 0]
    neg = [r for r in runs if r[0] < 0]
    if pos and neg:
        # all runs when few enough to read; else the longest of each sign.
        show_pos = (
            pos if len(pos) + len(neg) <= 4 else [max(pos, key=lambda r: r[2] - r[1])]
        )
        show_neg = (
            neg if len(pos) + len(neg) <= 4 else [max(neg, key=lambda r: r[2] - r[1])]
        )
        fmt_pos = ", ".join(f"[{a:g}°–{b:g}°]" for _, a, b in show_pos)
        fmt_neg = ", ".join(f"[{a:g}°–{b:g}°]" for _, a, b in show_neg)
        word_pos = "arcs" if len(show_pos) > 1 else "arc"
        word_neg = "arcs" if len(show_neg) > 1 else "arc"
        lines.append(
            f"verdict: admits on {word_pos} {fmt_pos}, rejects on {word_neg} "
            f"{fmt_neg} — the polarity term flips across the sweep (STUB)"
        )
    else:
        lines.append(
            "verdict: no rectification — polarity does not alternate "
            "around the shell (STUB)"
        )
    return lines
