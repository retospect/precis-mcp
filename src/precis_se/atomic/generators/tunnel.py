"""The ``tunnel-reservoirs`` scene feature: sheet | tube | sheet.

The nanoreactor's reservoir stage (docs/backlog/hexfold-nanoreactor-tube.md,
slice H1): a tube -- a plain ``(n, n)`` or a multi-rail ``rail_tube`` --
whose two ends seat into holes cut in flat reservoir sheets by
:func:`hexfold.opening.armchair_opening` and assembled by
:func:`hexfold.tunnel.tunnel_reservoirs`.  The bore is closed off from
the outside: the only paths into the lumen are through the mouths'
defect rings.  One shared twist about the tube axis is scanned for both
ends; each sheet is cut at its own best scale.

```json
{"op":"generate","generator":"hexfold_scene","name":"t",
 "params":{"tube":[6,6],
 "features":[{"name":"t","type":"tunnel-reservoirs","sheet":[30,30]}],
 "k_tether":1.0}}
```

``tube`` is ``[n, periods]`` for an armchair ``tube(n,n)`` (other
chiralities refuse by name) or ``{"rail": [rows, periods], "rails": N,
"rows": M, "pores": [[arc,u,v],...]}`` for a rail tube.  The feature
takes ``name``, ``type`` and ``sheet`` ([x_periods, row_pairs] per
reservoir sheet) only.  One tethered stick pass: both sheets tethered
to their own planes, the tube and junction free.  ``opening.defects``
findings per end carry the fused-pair count and the minted defect-ring
census; ``opening.face.open`` warns of seam faces that could not mint a
closed arc (a rail end's seam gaps).

Analytic seed plus a tethered preview relax: no stability claim.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

from hexfold.build import Net
from hexfold.check import Relaxed, geometry_findings
from hexfold.report import Finding, Report, Severity
from hexfold.stick import angle_springs, stick_relax_pinned
from hexfold.tunnel import Junction, Tunnel, tunnel_reservoirs
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import (
    _block_from_net,
    surviving_ordinals,
    terminate_mode,
)

FEATURE_TYPE = "tunnel-reservoirs"


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_pair(v: object, what: str) -> tuple[int, int]:
    if not isinstance(v, list | tuple) or len(v) != 2 or not all(_is_int(x) for x in v):
        raise GeneratorError(f"{what} must be a pair of integers [a, b]; got {v!r}")
    return int(v[0]), int(v[1])


def _normalize(
    params: dict[str, Any],
) -> tuple[tuple, tuple[int, int], str, float, str]:
    if set(params) - {"tube", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "tunnel-reservoirs scene accepts only tube, features, k_tether, "
            "terminate; no sheet, extra or mixed feet"
        )
    tube = params.get("tube")
    if (
        isinstance(tube, list | tuple)
        and len(tube) == 2
        and all(_is_int(v) for v in tube)
    ):
        kind = "armchair"
        n, periods = int(tube[0]), int(tube[1])
        if not 3 <= n <= 40 or not 2 <= periods <= 60:
            raise GeneratorError(
                "tunnel-reservoirs tube [n, periods] needs n in [3,40] and "
                f"periods in [2,60]; got {tube!r}"
            )
        tspec: tuple = (n, periods)
    elif isinstance(tube, dict):
        rail = tube.get("rail")
        wall = _int_pair(rail, "tube.rail") if rail is not None else None
        rails = tube.get("rails")
        rows = tube.get("rows", 4)
        pores = tube.get("pores", [])
        if (
            wall is None
            or not _is_int(rails)
            or not _is_int(rows)
            or not isinstance(pores, list)
            or not all(
                isinstance(p, list | tuple)
                and len(p) == 3
                and all(_is_int(x) for x in p)
                for p in pores
            )
        ):
            raise GeneratorError(
                "tunnel-reservoirs rail tube must be {rail: [rows, periods], "
                "rails: int, rows: int, pores: [[arc,u,v],...]}; got "
                f"{tube!r}"
            )
        kind = "rail"
        tspec = (wall[0], wall[1], rows, rails, tuple(tuple(p) for p in pores) or None)
    else:
        raise GeneratorError(
            "tunnel-reservoirs tube must be [n, periods] (armchair) or "
            "{rail:[rows,periods], rails, rows, pores} (rail tube); got "
            f"{tube!r}"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"tunnel-reservoirs scene requires exactly one {FEATURE_TYPE} "
            "feature; no mixed feet"
        )
    feature = features[0]
    if feature.get("type") != FEATURE_TYPE:
        raise GeneratorError(f"tunnel-reservoirs feature type must be {FEATURE_TYPE!r}")
    if set(feature) - {"name", "type", "sheet"}:
        raise GeneratorError(
            "tunnel-reservoirs feature takes only name, type, sheet; got "
            f"{sorted(map(str, set(feature) - {'name', 'type', 'sheet'}))}"
        )
    if "sheet" not in feature:
        raise GeneratorError("tunnel-reservoirs feature is missing 'sheet'")
    sheet = _int_pair(feature["sheet"], "sheet")
    if not 4 <= sheet[0] <= 80 or not 4 <= sheet[1] <= 80:
        raise GeneratorError(
            "tunnel-reservoirs sheet must be [x_periods, row_pairs] each in "
            f"[4,80]; got {sheet!r}"
        )
    name = feature.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("tunnel-reservoirs feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "tunnel-reservoirs k_tether must be finite and positive "
            "(tethered preview only)"
        )
    return tspec, sheet, name, float(k), kind


def _free_dir(pos: np.ndarray, nbrs: list[list[int]], a: int) -> np.ndarray:
    """Unit vector along an atom's missing-bond direction (away from its
    present neighbours' centroid)."""
    v = pos[a] - pos[nbrs[a]].mean(axis=0) if nbrs[a] else np.array([0, 0, 1.0])
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.array([0.0, 0.0, 1.0])


def _tunnel_sites(net: Net, tun: Tunnel, pos: np.ndarray) -> dict[str, Any]:
    """Labelled site list for the reservoir tunnel: every atom of each
    mouth junction (fused pair members, defect-face members, leftover
    dangling boundary atoms) plus, for a rail tube, the lumen's open
    rib-edge carbons and seam atoms -- the catalysis sites along the
    tunnel and at the mouths the nanoreactor doc asks to be addressable.

    Entries carry ``ordinal``, ``region`` (``junction_lo|hi``,
    ``strip<k>``, ``seam<k>``), ``role`` (``fused`` | ``unbonded`` |
    ``defect`` | ``lumen`` | ``seam``), cylindrical ``z_A`` / ``r_A`` /
    ``theta_deg`` about the tunnel axis and a ``normal`` pointing at the
    open volume.
    """
    nbrs: list[list[int]] = [[] for _ in net.atoms]
    deg = np.zeros(len(net.atoms), dtype=int)
    for i, j, _o in net.bonds:
        nbrs[i].append(j)
        nbrs[j].append(i)
        deg[i] += 1
        deg[j] += 1
    sites: list[dict[str, Any]] = []
    for tag, jun in (
        ("junction_lo", tun.junctions[0]),
        ("junction_hi", tun.junctions[1]),
    ):
        fused = {a for p in jun.fused for a in p}
        for h, r in jun.fused:
            for o, partner in ((h, r), (r, h)):
                v = pos[partner] - pos[o]
                sites.append(
                    {
                        "ordinal": int(o),
                        "region": tag,
                        "role": "fused",
                        "normal": (v / np.linalg.norm(v)).tolist()
                        if float(np.linalg.norm(v)) > 1e-9
                        else [0.0, 0.0, 0.0],
                    }
                )
        for a in jun.unbonded_sheet + jun.unbonded_tube:
            sites.append(
                {
                    "ordinal": int(a),
                    "region": tag,
                    "role": "unbonded",
                    "normal": _free_dir(pos, nbrs, a).tolist(),
                }
            )
        for a in (
            {x for f in jun.faces for x in f}
            - fused
            - set(jun.unbonded_sheet + jun.unbonded_tube)
        ):
            sites.append(
                {
                    "ordinal": int(a),
                    "region": tag,
                    "role": "defect",
                    "normal": _free_dir(pos, nbrs, a).tolist(),
                }
            )
    junction_atoms = {e["ordinal"] for e in sites}
    if tun.kind == "rail":
        for kk, strip in enumerate(tun.strip_atoms):
            for o in strip:
                if deg[o] < 3 and o not in junction_atoms:
                    sites.append(
                        {
                            "ordinal": int(o),
                            "region": f"strip{kk}",
                            "role": "lumen",
                            "normal": _free_dir(pos, nbrs, o).tolist(),
                        }
                    )
        for kk, seam in enumerate(tun.seam_atoms):
            for o in seam:
                if o not in junction_atoms:
                    rxy = float(np.linalg.norm(pos[o][:2]))
                    inward = (
                        [-pos[o, 0] / rxy, -pos[o, 1] / rxy, 0.0]
                        if rxy > 1e-9
                        else [0.0, 0.0, 0.0]
                    )
                    sites.append(
                        {
                            "ordinal": int(o),
                            "region": f"seam{kk}",
                            "role": "seam",
                            "normal": inward,
                        }
                    )
    for e in sites:
        x, y, z = (float(v) for v in pos[e["ordinal"]])
        e["z_A"] = z
        e["r_A"] = float(math.hypot(x, y))
        e["theta_deg"] = float(math.degrees(math.atan2(y, x)))
    # pendant pairs: two open-valence lumen/junction atoms close enough to
    # share one adsorbate
    lo, hi = 2.5, 3.0
    open_sites = [e for e in sites if e["role"] in ("lumen", "unbonded")]
    pairs = [
        [a["ordinal"], b["ordinal"]]
        for i, a in enumerate(open_sites)
        for b in open_sites[i + 1 :]
        if lo <= float(np.linalg.norm(pos[a["ordinal"]] - pos[b["ordinal"]])) <= hi
    ]
    return {"sites": sites, "pairs": pairs}


def build_tunnel_reservoirs(params: dict[str, Any]) -> GeneratedBlock:
    tspec, sheet, name, k, kind = _normalize(params)
    termination = terminate_mode(params)
    try:
        net, tun = tunnel_reservoirs(tspec, sheet, kind=kind)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    seed = tun.coords

    zlo, zhi = tun.sheet_plane_z

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot = pos.copy()
        normal = np.zeros_like(pos)
        for idx, z0 in ((tun.sheet_lo, zlo), (tun.sheet_hi, zhi)):
            atoms = np.asarray(idx, dtype=np.int64)
            foot[atoms] = pos[atoms] - (pos[atoms, 2] - z0)[:, None] * np.array(
                [0.0, 0.0, 1.0]
            )
            normal[atoms] = np.array([0.0, 0.0, 1.0])
        return foot, normal

    movable = np.ones(len(net.atoms), dtype=float)
    raw, force = stick_relax_pinned(
        np.asarray(seed, dtype=np.float64),
        np.asarray([(i, j) for i, j, _ in net.bonds], dtype=np.int64),
        np.full(len(net.bonds), net.lattice.sigma_A),
        np.asarray(angle_springs(net), dtype=float),
        net.lattice.sigma_A,
        movable=movable,
        tether=tether,
        k_tether=k,
    )
    pos = np.asarray(raw, dtype=float)
    if not np.isfinite(pos).all() or not math.isfinite(force):
        raise GeneratorError(
            "tunnel-reservoirs tethered relaxation returned nonfinite coordinates/force"
        )
    findings = list(net.report.findings)
    findings.extend(geometry_findings(net, relaxed=Relaxed(pos, force, "tethered")))
    site_data = _tunnel_sites(net, tun, pos)
    findings.append(
        Finding(
            "sites",
            Severity.INFO,
            f"{len(site_data['sites'])} labelled mouth/lumen sites "
            f"({len(site_data['pairs'])} pendant pairs)",
            where=name,
            data=(
                ("count", len(site_data["sites"])),
                ("pairs", site_data["pairs"]),
            ),
        )
    )
    normalized: dict[str, Any] = {
        "tube": (
            list(tspec)
            if kind == "armchair"
            else {
                "rail": list(tspec[:2]),
                "rails": tspec[3],
                "rows": tspec[2],
                "pores": [list(p) for p in (tspec[4] or ())],
            }
        ),
        "features": [{"name": name, "type": FEATURE_TYPE, "sheet": list(sheet)}],
        "k_tether": k,
    }
    if "terminate" in params:
        normalized["terminate"] = termination
    block = _block_from_net(
        net,
        pos,
        spec="",
        terminate=termination,
        report=Report(tuple(findings)).sorted(),
        fidelity="stick",
        extra_topology={
            "scene": normalized,
            "plan": {
                "construction": (
                    f"sheet | tube | sheet: {kind} tube ends seated into "
                    f"reservoir sheets {sheet[0]}x{sheet[1]} by the "
                    f"armchair opening solver (shared twist "
                    f"{tun.twist_deg:.0f} deg)"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "joint_constraint": (
                    "reservoir sheets tethered to their planes; tube and junction free"
                ),
                "surface_deviation": (
                    "unavailable: two sheets plus a tube is not a surface of revolution"
                ),
            },
        },
        provenance_tail=(
            f" sheet|tube|sheet reservoirs, shared twist {tun.twist_deg:.0f} "
            "deg, one tethered stick pass (sheets planar-tethered, junction "
            "free); replay generated.scene params; no stability claim"
        ),
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    # sites are measured on the relaxed authored frame (tunnel axis at
    # x=y=0); ``frame.to_stored`` is the affine map into the stored
    # coordinates for round-tripping against stored atoms. A rim atom the
    # terminator trimmed is gone from the block: fit on the survivors and
    # move the site and junction records onto stored ordinals.
    remap = surviving_ordinals(net, block)
    kept = sorted(remap, key=remap.__getitem__)
    design = np.hstack([pos[kept], np.ones((len(kept), 1))])
    xf, *_ = np.linalg.lstsq(design, block.coords[: len(kept)], rcond=None)
    site_data["sites"] = [
        {**e, "ordinal": remap[e["ordinal"]]}
        for e in site_data["sites"]
        if e["ordinal"] in remap
    ]
    site_data["pairs"] = [
        [remap[a], remap[b]] for a, b in site_data["pairs"] if a in remap and b in remap
    ]
    site_data["frame"] = {
        "axis": "tunnel axis, +z through the mouth centres",
        "origin_A": [0.0, 0.0, 0.0],
        "theta_zero": "+x",
        "measured_on": "relaxed coordinates before canonical framing",
        "to_stored": {
            "rot": xf[:3].tolist(),
            "shift_A": xf[3].tolist(),
        },
    }
    block.topology["sites"] = site_data
    stored_findings = list(block.topology["report"]["findings"])
    stored_findings.extend(
        f.to_dict()
        for f in _junction_checks(
            block, net, stored_tunnel(net, tun, block), termination
        )
    )
    block.topology["report"]["findings"] = stored_findings
    return block


def tunnel_strain(
    coords: np.ndarray,
    elements: Sequence[str] | np.ndarray,
    bonds: Iterable[tuple[int, int, Any, Any]],
    junctions: Sequence[Junction],
    seam_atoms: Sequence[Sequence[int]],
    sigma: float,
) -> list[Finding]:
    """Strain read on any coordinate set: one ``junction.strain`` INFO per
    junction (seam-bond and zone-bond lengths, worst face-angle deviation,
    degree range) plus one ``seam.strain`` INFO per rail seam chain in
    ``seam_atoms`` (lengths of the C-C bonds incident to the chain, worst
    C-C-C angle deviation from 120 deg, worst pyramidalisation at degree-3 seam atoms; ``None`` where a chain
    has no incident C-C bond or no degree-3 atom).

    Pure over its arguments -- it needs neither the Net nor the stick seed,
    so it is the read to re-run on relaxed coordinates (stick now; the
    xtb/MLIP relax of H3 runs on the compute group).  ``bonds`` are
    ``GeneratedBlock.bonds`` tuples ``(i, j, order, kind)``; ``coords``,
    ``elements`` and ordinals share the stored-block indexing.
    """
    xyz = np.asarray(coords, dtype=float)
    is_c = np.asarray(elements) == "C"
    blist = list(bonds)
    cb: list[set[int]] = [set() for _ in range(len(xyz))]
    for i, j, _o, _k in blist:
        if is_c[i] and is_c[j]:
            cb[i].add(j)
            cb[j].add(i)
    out: list[Finding] = []
    for tag, jun in zip(("low", "high"), junctions, strict=False):
        jset = set(jun.atoms)
        degs = [len(cb[a]) for a in jun.atoms]
        seam_bl = [float(np.linalg.norm(xyz[h] - xyz[r])) for h, r in jun.fused]
        zone_bl = [
            float(np.linalg.norm(xyz[i] - xyz[j]))
            for i, j, _o, _k in blist
            if (i in jset or j in jset) and is_c[i] and is_c[j]
        ]
        # max interior-angle deviation inside the minted faces
        worst_angle = 0.0
        for f in jun.faces:
            n = len(f)
            ideal = (n - 2) * 180.0 / n
            for k in range(n):
                a, v, b = f[k - 1], f[k], f[(k + 1) % n]
                va = xyz[a] - xyz[v]
                vb = xyz[b] - xyz[v]
                na, nb = np.linalg.norm(va), np.linalg.norm(vb)
                if na < 1e-9 or nb < 1e-9:
                    continue
                ang = math.degrees(
                    math.acos(float(np.clip(va @ vb / (na * nb), -1, 1)))
                )
                worst_angle = max(worst_angle, abs(ang - ideal))
        out.append(
            Finding(
                "junction.strain",
                Severity.INFO,
                f"{tag} junction: {len(jun.fused)} seam bonds "
                f"{min(seam_bl):.2f}-{max(seam_bl):.2f} A (ideal {sigma:.2f}); "
                f"zone bonds {min(zone_bl):.2f}-{max(zone_bl):.2f} A; "
                f"max face-angle deviation {worst_angle:.1f} deg; "
                f"degree min/max {min(degs)}/{max(degs)}",
                where=tag,
                data=(
                    ("seam_bonds_min_A", min(seam_bl)),
                    ("seam_bonds_max_A", max(seam_bl)),
                    ("seam_bonds_mean_A", float(np.mean(seam_bl))),
                    ("zone_bonds_min_A", min(zone_bl)),
                    ("zone_bonds_max_A", max(zone_bl)),
                    ("angle_dev_max_deg", worst_angle),
                    ("degree_min", min(degs)),
                    ("degree_max", max(degs)),
                ),
            )
        )
    for kk, seam in enumerate(seam_atoms):
        # rail seam atoms are every-other atoms of the seam line (second
        # neighbours, ~2.5 A apart), so consecutive chain atoms are never
        # bonded: read the C-C bonds incident to the chain instead
        sset = set(seam)
        lens = [
            float(np.linalg.norm(xyz[i] - xyz[j]))
            for i, j, _o, _k in blist
            if (i in sset or j in sset) and is_c[i] and is_c[j]
        ]
        ang_dev: list[float] = []
        pyr: list[float] = []
        for a in seam:
            if len(cb[a]) != 3:
                continue
            nb = sorted(cb[a])
            v = xyz[nb] - xyz[a]
            vl = np.linalg.norm(v, axis=1)
            if (vl < 1e-9).any():
                continue
            u = v / vl[:, None]
            ang = np.degrees(np.arccos(np.clip(u @ u.T, -1, 1)))
            ang_dev.append(float(np.abs(ang[np.triu_indices(3, 1)] - 120.0).max()))
            pn = np.cross(xyz[nb[1]] - xyz[nb[0]], xyz[nb[2]] - xyz[nb[0]])
            nn = np.linalg.norm(pn)
            if nn > 1e-9:
                pyr.append(float(abs((xyz[a] - xyz[nb[0]]) @ pn) / nn))
        bmin = min(lens) if lens else None
        bmax = max(lens) if lens else None
        bmean = float(np.mean(lens)) if lens else None
        adev = max(ang_dev) if ang_dev else None
        pmax = max(pyr) if pyr else None

        def fmt(x: float | None, spec: str) -> str:
            return "n/a" if x is None else format(x, spec)

        out.append(
            Finding(
                "seam.strain",
                Severity.INFO,
                f"seam {kk}: {len(seam)} atoms, bonds "
                f"{fmt(bmin, '.2f')}-{fmt(bmax, '.2f')} A (ideal {sigma:.2f}); "
                f"max angle deviation {fmt(adev, '.1f')} deg; "
                f"max pyramidalisation {fmt(pmax, '.2f')} A",
                where=f"seam{kk}",
                data=(
                    ("bonds_min_A", bmin),
                    ("bonds_max_A", bmax),
                    ("bonds_mean_A", bmean),
                    ("angle_dev_max_deg", adev),
                    ("pyramidal_max_A", pmax),
                    ("atoms", len(seam)),
                ),
            )
        )
    return out


def stored_tunnel(net: Net, tun: Tunnel, block: GeneratedBlock) -> Tunnel:
    """``tun`` with every ordinal moved onto the stored ``block``: the
    terminator may trim rim atoms at a mouth (gr486406), which shifts the
    survivors down, so the authored composite ordinals would read the
    wrong atoms. A trimmed atom drops out of each list (and a fused pair
    with it)."""
    remap = surviving_ordinals(net, block)

    def ords(seq: Iterable[int]) -> tuple[int, ...]:
        return tuple(remap[o] for o in seq if o in remap)

    def junction(j: Junction) -> Junction:
        return Junction(
            fused=tuple(
                (remap[s], remap[t]) for s, t in j.fused if s in remap and t in remap
            ),
            faces=tuple(ords(f) for f in j.faces),
            unbonded_sheet=ords(j.unbonded_sheet),
            unbonded_tube=ords(j.unbonded_tube),
            atoms=ords(j.atoms),
        )

    kept = sorted(remap, key=remap.__getitem__)
    return dataclasses.replace(
        tun,
        sheet_lo=ords(tun.sheet_lo),
        sheet_hi=ords(tun.sheet_hi),
        tube_atoms=ords(tun.tube_atoms),
        junctions=(junction(tun.junctions[0]), junction(tun.junctions[1])),
        coords=np.asarray(tun.coords)[kept],
        strip_atoms=tuple(ords(s) for s in tun.strip_atoms),
        seam_atoms=tuple(ords(s) for s in tun.seam_atoms),
    )


def _junction_checks(
    block: GeneratedBlock, net: Net, tun: Tunnel, termination: str
) -> list[Finding]:
    """Slice-4 validation on the *stored* block (H caps included):
    no dangling carbon without a cap, no contact under 1.2 A, coordination
    inside each junction, and a per-junction strain read (see :func:`tunnel_strain`)."""
    out: list[Finding] = []
    coords = np.asarray(block.coords)
    elements = np.asarray(block.elements)
    is_c = elements == "C"
    cbonds: list[list[int]] = [[] for _ in range(len(coords))]
    bonded_pairs: set[frozenset[int]] = set()
    for i, j, _o, _k in block.bonds:
        bonded_pairs.add(frozenset((i, j)))
        if is_c[i] and is_c[j]:
            cbonds[i].append(j)
            cbonds[j].append(i)
    alln: list[list[int]] = [[] for _ in range(len(coords))]
    for i, j, _o, _k in block.bonds:
        alln[i].append(j)
        alln[j].append(i)
    # 1. dangling-carbon coverage: every C short of sp2 needs an H cap
    uncovered = []
    if termination != "none":
        for a in np.where(is_c)[0]:
            if len(cbonds[a]) < 3 and not any(not is_c[b] for b in alln[a]):
                uncovered.append(int(a))
    if uncovered:
        out.append(
            Finding(
                "termination.uncovered",
                Severity.WARN,
                f"{len(uncovered)} carbon(s) short of valence carry no H cap",
                where="",
                data=(("ordinals", uncovered),),
            )
        )
    else:
        out.append(
            Finding(
                "termination.covered",
                Severity.INFO,
                "every carbon short of valence carries an H cap",
                where="",
            )
        )
    # 2. sub-1.2 A nonbonded contacts across the whole block
    from scipy.spatial import cKDTree

    tree = cKDTree(coords)
    hits = [
        (i, j, float(np.linalg.norm(coords[i] - coords[j])))
        for i, j in tree.query_pairs(1.2)
        if frozenset((i, j)) not in bonded_pairs
    ]
    if hits:
        out.append(
            Finding(
                "geom.contact.short",
                Severity.WARN,
                f"{len(hits)} nonbonded pair(s) under 1.2 A "
                f"(worst {min(w[2] for w in hits):.2f} A)",
                data=(("pairs", sorted(hits, key=lambda w: w[2])[:20]),),
            )
        )
    else:
        out.append(
            Finding(
                "geom.contacts",
                Severity.INFO,
                "no nonbonded contacts under 1.2 A",
            )
        )
    # 3. per-junction coordination + strain on stored coords
    strain = tunnel_strain(
        coords,
        elements,
        block.bonds,
        tun.junctions,
        tun.seam_atoms if tun.kind == "rail" else (),
        net.lattice.sigma_A,
    )
    strain_by_tag: dict[str, list[Finding]] = {}
    seam_strain: list[Finding] = []
    for f in strain:
        if f.code == "junction.strain":
            strain_by_tag.setdefault(f.where, []).append(f)
        else:
            seam_strain.append(f)
    for tag, jun in zip(("low", "high"), tun.junctions, strict=True):
        over = [a for a in jun.atoms if len(cbonds[a]) > 3]
        under = [a for a in jun.atoms if len(cbonds[a]) < 2]
        if over or under:
            out.append(
                Finding(
                    "junction.coordination",
                    Severity.WARN,
                    f"{tag} junction: {len(over)} atom(s) degree>3, "
                    f"{len(under)} degree<2",
                    where=tag,
                    data=(("over", sorted(over)), ("under", sorted(under))),
                )
            )
        out.extend(strain_by_tag.get(tag, ()))
        # relax research (memory me482127): an equal-120 sp2 seam dimerises
        # into sp3 under GFN2-xTB -- flag a run of flat equal-120 degree-3
        # junction atoms so the pattern is never silently emitted
        flat120: set[int] = set()
        for a in jun.atoms:
            nb = cbonds[a]
            if len(nb) != 3:
                continue
            v = coords[nb] - coords[a]
            vl = np.linalg.norm(v, axis=1)
            if (vl < 1e-9).any():
                continue
            u = v / vl[:, None]
            ang = np.degrees(np.arccos(np.clip(u @ u.T, -1, 1)))
            if (
                np.abs(ang[np.triu_indices(3, 1)] - 120.0).max() <= 10.0
                and abs(float(np.linalg.det(u))) < 0.15
            ):
                flat120.add(int(a))
        # contiguous runs along the minted seam
        seen: set[int] = set()
        runs = 0
        for a in sorted(flat120):
            if a in seen:
                continue
            run = {a}
            stack = [a]
            while stack:
                x = stack.pop()
                for y in cbonds[x]:
                    if y in flat120 and y not in run:
                        run.add(y)
                        stack.append(y)
            seen |= run
            if len(run) >= 3:
                runs += 1
        if runs:
            out.append(
                Finding(
                    "junction.sp2_seam",
                    Severity.WARN,
                    f"{tag} junction: {runs} run(s) of flat equal-120 "
                    "degree-3 seam atoms — an equal-120 sp2 seam dimerises "
                    "into sp3 under GFN2-xTB; the defect ring should break "
                    "it or the seam atoms belong on a dimer pair",
                    where=tag,
                    data=(("runs", runs), ("flat120_atoms", len(flat120))),
                )
            )
    out.extend(seam_strain)
    # rail seams: each seam chain must end terminated — fused into the
    # junction or carrying a cap; an open end is never emitted
    if tun.kind == "rail":
        fused_ords = {a for j in tun.junctions for p in j.fused for a in p}
        open_ends = []
        for kk, seam in enumerate(tun.seam_atoms):
            for o in (seam[0], seam[-1]):
                if o in fused_ords or len(cbonds[o]) >= 3:
                    continue
                capped = (
                    any(not is_c[b] for b in alln[o])
                    if termination != "none"
                    else False
                )
                if not capped:
                    open_ends.append((kk, int(o)))
        if open_ends:
            out.append(
                Finding(
                    "seam.end.open",
                    Severity.WARN,
                    f"{len(open_ends)} seam chain end(s) neither fused into "
                    "a junction nor capped — every seam end needs a "
                    "terminator",
                    data=(("ends", open_ends),),
                )
            )
        else:
            out.append(
                Finding(
                    "seam.ends",
                    Severity.INFO,
                    f"all {2 * len(tun.seam_atoms)} seam ends terminated "
                    "(fused or capped)",
                )
            )
    # lone pentagons/heptagons: a non-hex face sharing no edge with another
    # non-hex face is an isolated defect in a flat sheet — the known weak
    # point; defects minted in the junction ring travel in clusters
    edges_of: dict[frozenset[int], list[int]] = {}
    nonhex: list[tuple[int, list[int]]] = []
    for ri, ring in enumerate(net.rings):
        if len(ring) == 6:
            continue
        nonhex.append((ri, list(ring)))
        for k in range(len(ring)):
            edges_of.setdefault(
                frozenset((ring[k], ring[(k + 1) % len(ring)])), []
            ).append(ri)
    lone = []
    for ri, members in nonhex:
        n_ = len(members)
        adjacent = any(
            ri2 != ri
            for k in range(n_)
            for ri2 in edges_of.get(frozenset((members[k], members[(k + 1) % n_])), [])
        )
        if not adjacent:
            lone.append((n_, members))
    if lone:
        out.append(
            Finding(
                "sheet.defect.lone",
                Severity.WARN,
                f"{len(lone)} lone non-hex face(s) share no edge with "
                "another defect — an isolated pentagon/heptagon in a flat "
                "unpinned sheet is the known strain site",
                data=(
                    ("sizes", [s for s, _r in lone]),
                    ("rings", [r for _s, r in lone[:20]]),
                ),
            )
        )
    return out
