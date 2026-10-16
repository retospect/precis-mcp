"""``opening(port=X)@site``: a sheet hole solved from a tube's rim (SPEC 11.4).

The grammar line ``a: sheet(20,20) - opening(port=t.in)@(10,10,A)`` *is*
the join -- no ``fuse`` line.  :func:`build_openings` builds the spec with
the opening holes removed (sheet and tube are then separate components),
places the parts rigidly, and lets :mod:`hexfold.opening` cut the sheet
hole and stitch the rim in, the way :func:`hexfold.tunnel.tunnel_reservoirs`
does for its fixed sheet | tube | sheet stack.

Placement, per opening in spec order (components = bond-graph pieces):

- neither side placed, or the sheet placed: the *tube* component moves
  rigidly, rim axis along the sheet normal, rim centroid over the opening
  site, rim dangling plane one sigma past the sheet plane on the tube
  side; the tube twist about its axis is scanned.
- the tube already placed (a second end): the *sheet* component moves,
  plane one sigma beyond the rim, opening site under the rim centroid;
  the sheet twist about the opening is scanned.

Armchair ``tube(n,n)`` rims only: a zigzag or chiral rim is refused by
name (``opening.rim.unsupported``), never approximated.  Analytic seed
only -- callers relax via stick.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from .build import Atom, Net, _compute_sheets
from .join import Block
from .opening import (
    SEAT_SIGMA,
    Opening,
    RimLoop,
    apply_opening,
    rim_loop_port,
    scan_opening,
    twist_coords,
)
from .report import BuildError, Finding, Profile, Report, Severity
from .text import OPENING_RING, Hole, Spec

#: degrees scanned for the twist of the moved part about the mouth axis
_TWISTS = tuple(range(0, 60, 2))
#: the sheet must clear the rim polygon by this margin (Angstrom)
_MARGIN = 3.0

_ZIGZAG_DOC = "docs/backlog/hexfold-nanoreactor-tube.md"


def _err(code: str, msg: str, where: str = "", span: tuple[int, int] | None = None):
    return Finding(code, Severity.ERROR, msg, where=where, span=span)


def _tube_nm(params: tuple[tuple[str, str], ...]) -> tuple[int, int] | None:
    kv = dict(params)
    try:
        return int(kv.get("0", kv.get("n", ""))), int(kv.get("1", kv.get("m", "")))
    except ValueError:
        return None


def _rot_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation matrix taking unit vector ``a`` onto unit vector ``b``."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    c = float(a @ b)
    if c > 1.0 - 1e-12:
        return np.eye(3)
    if c < -1.0 + 1e-12:
        # half turn about any axis perpendicular to a
        ref = (
            np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        )
        ax = np.cross(a, ref)
        ax /= np.linalg.norm(ax)
        return 2.0 * np.outer(ax, ax) - np.eye(3)
    v = np.cross(a, b)
    k = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + k + k @ k / (1.0 + c)


def _components(n: int, bonds: tuple[tuple[int, int, int], ...]) -> list[int]:
    parent = list(range(n))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in bonds:
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[ri] = rj
    return [root(x) for x in range(n)]


def _sub_block(net: Net, pos: np.ndarray, idx: list[int], sigma: float) -> Block:
    """The block of ordinals ``idx`` (ascending), bonds/rings re-indexed."""
    remap = {o: k for k, o in enumerate(idx)}
    return Block(
        elements=tuple(net.atoms[o].element for o in idx),
        coords=pos[idx].copy(),
        bonds=tuple(
            (remap[i], remap[j], o)
            for i, j, o in net.bonds
            if i in remap and j in remap
        ),
        rings=tuple(
            tuple(remap[a] for a in r) for r in net.rings if set(r) <= remap.keys()
        ),
        ports={},
        sigma=sigma,
    )


def build_openings(
    spec: Spec,
    profile: Profile,
    strict: bool,
    extra: list[Finding],
    build_fn: Callable[..., Net],
) -> Net:
    """Build ``spec`` whose sheets carry ``opening(port=...)`` holes."""
    openings: list[tuple[str, Hole]] = []
    stripped_insts = []
    for inst in spec.instances:
        mine = [h for h in inst.holes if h.port is not None]
        openings.extend((inst.name, h) for h in mine)
        stripped_insts.append(
            replace(inst, holes=tuple(h for h in inst.holes if h.port is None))
            if mine
            else inst
        )
    base = build_fn(
        replace(spec, instances=tuple(stripped_insts)), profile=profile, strict=False
    )
    findings: list[Finding] = list(base.report.findings) + list(extra)

    def finish(net: Net) -> Net:
        report = profile.apply_all(findings)
        net = replace(net, report=report, spec=spec)
        if strict and not report.ok:
            raise BuildError(report)
        return net

    if spec.connects or spec.seams or spec.registry or spec.terminate:
        findings.append(
            _err(
                "opening.combined",
                "opening(port=) does not compose with fuse/bond/seam/terminate "
                "lines yet; build the opening alone",
            )
        )
        return finish(base)
    fit = [i.name for i in spec.instances if any(v == "fit" for _, v in i.params)]
    if fit:
        findings.append(
            _err(
                "opening.fit",
                f"opening(port=) does not combine with fit parameters ({', '.join(fit)}): "
                "fit runs without the hole and cannot promise the cut; "
                "give explicit values",
            )
        )
        return finish(base)
    sigma = base.lattice.sigma_A
    n_at = len(base.atoms)
    ports = dict(base.ports)
    comp = _components(n_at, base.bonds)
    pos = np.asarray(base.seed3, dtype=float).copy()

    # validate every opening before moving anything
    plan: list[tuple[str, Hole, str, RimLoop]] = []
    refused = False
    for host, hole in openings:
        where = f"{host}:opening(port={hole.port})"
        label = f"opening(port={hole.port})"
        assert hole.port is not None
        tname = hole.port.rsplit(".", 1)[0]
        hinst, tinst = spec.instance(host), spec.instance(tname)
        if hole.port not in ports or tinst is None:
            findings.append(_err("port.unknown", f"{label}: no such port", where))
            refused = True
            continue
        if hinst is None or hinst.kind != "sheet":
            findings.append(
                _err(
                    "opening.rim.unsupported",
                    f"{label}: host {host} is a "
                    f"{hinst.kind if hinst else '?'}; only a sheet can carry an opening",
                    where,
                )
            )
            refused = True
            continue
        nm = _tube_nm(tinst.params) if tinst.kind == "tube" else None
        if tinst.kind != "tube" or nm is None:
            findings.append(
                _err(
                    "opening.rim.unsupported",
                    f"{label}: {tname} is a {tinst.kind}, not a tube end",
                    where,
                )
            )
            refused = True
            continue
        n, m = nm
        if m == 0:
            msg = (
                f"{label}: zigzag rim tube({n},{m}) is not supported yet — "
                f"armchair tube(n,n) rims only (zigzag rims: {_ZIGZAG_DOC})"
            )
        elif m != n:
            msg = (
                f"{label}: chiral rim tube({n},{m}) is not supported yet — "
                f"armchair tube(n,n) rims only (see {_ZIGZAG_DOC})"
            )
        else:
            msg = ""
        if msg:
            findings.append(_err("opening.rim.unsupported", msg, where))
            refused = True
            continue
        try:
            plan.append((host, hole, tname, rim_loop_port(base, hole.port)))
        except Exception as exc:
            findings.append(_err("opening.scan.failed", f"{label}: {exc}", where))
            refused = True
    if refused:
        return finish(base)

    placed: set[int] = set()  # component roots whose geometry is fixed
    comp_of = list(comp)  # mutable: roots merge as openings join parts
    removed: set[int] = set()
    new_bonds: list[tuple[int, int, int]] = []
    new_rings: list[tuple[int, ...]] = []
    used_ports: set[str] = set()
    info: list[Finding] = []

    def members(root: int) -> list[int]:
        return [o for o in range(n_at) if comp_of[o] == root]

    for host, hole, tname, rim in plan:
        label = f"opening(port={hole.port})"
        where = f"{host}:{label}"
        assert hole.port is not None
        site_atoms = [
            a.ord
            for a in base.atoms
            if a.instance == host
            and a.path.site == hole.site
            and a.path.defect is None
            and not a.path.h
        ]
        if not site_atoms or site_atoms[0] in removed:
            findings.append(
                _err(
                    "hole.missing",
                    f"{label}@{hole.site}: no such site on {host}",
                    where,
                )
            )
            return finish(base)
        site_ord = site_atoms[0]
        cs, ct = comp_of[site_ord], comp_of[rim.atoms[0]]
        if cs == ct:
            findings.append(
                _err(
                    "opening.placement",
                    f"{label}: {host} and {tname} are already one piece "
                    "(a second opening between them would close a ring)",
                    where,
                )
            )
            return finish(base)
        if cs in placed and ct in placed:
            findings.append(
                _err(
                    "opening.placement",
                    f"{label}: both {host} and {tname} are already placed by "
                    "earlier openings",
                    where,
                )
            )
            return finish(base)
        tube_ords = [o for o in range(n_at) if base.atoms[o].instance == tname]
        s_ords = [o for o in members(cs) if base.atoms[o].instance == host]
        rim_cen = pos[list(rim.atoms)].mean(axis=0)
        outward = rim_cen - pos[tube_ords].mean(axis=0)

        if ct not in placed:
            # move the tube component onto the sheet
            movers = members(ct)
            z_sheet = float(pos[s_ords][:, 2].mean())
            rot = _rot_between(outward, np.array([0.0, 0.0, 1.0]))
            pivot = pos[tube_ords].mean(axis=0)
            pos[movers] = (pos[movers] - pivot) @ rot.T + pivot
            rim_cen = pos[list(rim.atoms)].mean(axis=0)
            target = np.array([*pos[site_ord][:2], z_sheet - SEAT_SIGMA * sigma])
            pos[movers] += target - rim_cen
            mover, axis_pt = movers, pos[list(rim.atoms)].mean(axis=0)
        else:
            # the tube is fixed: move the sheet component under its rim
            movers = members(cs)
            ou = float(outward[2] / np.linalg.norm(outward))
            if abs(ou) < 0.95:
                findings.append(
                    _err(
                        "opening.rim.unsupported",
                        f"{label}: {tname}'s rim axis is not normal to the "
                        "sheet plane (placed by an earlier opening)",
                        where,
                    )
                )
                return finish(base)
            side = 1.0 if ou > 0 else -1.0
            z_sheet = float(pos[rim.atoms, 2].mean()) + side * SEAT_SIGMA * sigma
            shift = np.array([*(rim_cen[:2] - pos[site_ord][:2]), 0.0])
            pos[movers] += shift
            pos[movers, 2] += z_sheet - float(pos[s_ords][:, 2].mean())
            mover, axis_pt = movers, pos[site_ord].copy()

        # the sheet must clear the rim polygon by a margin on every side
        rp = pos[list(rim.atoms)]
        rad = float(np.linalg.norm(rp[:, :2] - rp[:, :2].mean(axis=0), axis=1).max())
        lo, hi = pos[s_ords][:, :2].min(axis=0), pos[s_ords][:, :2].max(axis=0)
        c2 = rp[:, :2].mean(axis=0)
        if np.any(c2 - lo < rad + _MARGIN) or np.any(hi - c2 < rad + _MARGIN):
            findings.append(
                _err(
                    "opening.sheet.small",
                    f"{label}@{hole.site}: sheet {host} is too small to seat "
                    f"the mouth (needs {rad + _MARGIN:.1f} A around the site)",
                    where,
                )
            )
            return finish(base)

        host_ords = [o for o in s_ords if o not in removed]
        sheet_blk0 = _sub_block(base, pos, host_ords, sigma)
        tube_blk = Block(
            elements=tuple(a.element for a in base.atoms),
            coords=pos,
            bonds=base.bonds,
            rings=base.rings,
            ports={},
            sigma=sigma,
        )
        best: tuple[float, np.ndarray, np.ndarray] | None = None
        axis = np.array([0.0, 0.0, 1.0])
        # the moved part's twist about the mouth axis; the tube may be fixed
        # only by an earlier opening, so then the sheet turns instead
        for tw in _TWISTS:
            trial = pos.copy()
            trial[mover] = twist_coords(pos[mover], tw, axis, axis_pt)
            blk = (
                sheet_blk0
                if ct not in placed
                else _sub_block(base, trial, host_ords, sigma)
            )
            try:
                got = scan_opening(blk, rim, trial)
            except Exception as exc:
                findings.append(_err("opening.scan.failed", f"{label}: {exc}", where))
                return finish(base)
            if got is None:
                continue
            if best is None or got[0] < best[0]:
                best = (got[0], trial, got[2])
        if best is None:
            findings.append(
                _err(
                    "opening.scan.failed",
                    f"{label}@{hole.site}: no twist/scale left a sheet boundary "
                    f"of {len(rim.atoms)} bondable atoms for this rim",
                    where,
                )
            )
            return finish(base)
        _cost, pos, polygon = best
        blk = _sub_block(base, pos, host_ords, sigma)
        tube_blk = replace(tube_blk, coords=pos)
        try:
            _cut, op = apply_opening(blk, tube_blk, rim, pos, polygon)
        except Exception as exc:
            findings.append(_err("opening.scan.failed", f"{label}: {exc}", where))
            return finish(base)
        rm_local = set(op.removed)
        kept = [o for k, o in enumerate(host_ords) if k not in rm_local]
        removed |= {host_ords[k] for k in rm_local}
        new_bonds += [(kept[h], r, 1) for h, r in op.fused]
        new_rings += [
            tuple(kept[a] if side == "s" else a for side, a in f) for f in op.faces
        ]
        used_ports.add(hole.port)
        # the join merges the two pieces
        for o in range(n_at):
            if comp_of[o] == ct:
                comp_of[o] = cs
        placed.add(cs)
        info += _opening_findings(op, hole, where)

    # ---- merge into one Net -------------------------------------------
    keep = [o for o in range(n_at) if o not in removed]
    remap = {o: k for k, o in enumerate(keep)}
    atoms = tuple(
        Atom(
            base.atoms[o].path,
            remap[o],
            base.atoms[o].element,
            base.atoms[o].hyb,
            base.atoms[o].instance,
        )
        for o in keep
    )
    bonds = tuple(
        sorted(
            [
                (remap[i], remap[j], o)
                for i, j, o in base.bonds
                if i in remap and j in remap
            ]
            + [(remap[i], remap[j], o) for i, j, o in new_bonds]
        )
    )
    rings = tuple(
        sorted(
            {
                tuple(remap[a] for a in r)
                for r in list(base.rings) + new_rings
                if all(a in remap for a in r)
            }
        )
    )
    new_ports = tuple(
        (
            name,
            replace(
                p,
                atoms=tuple(remap[a] for a in p.atoms),
                dangling=tuple(remap[a] for a in p.dangling),
            ),
        )
        for name, p in base.ports
        if name not in used_ports and all(a in remap for a in p.atoms)
    )
    sheet_atoms = _compute_sheets(list(atoms), list(bonds), set(), set())
    sheets = tuple(
        (name, tuple(i for i, r in enumerate(rings) if set(r) <= set(ords)))
        for name, ords in sheet_atoms
    )
    findings += info
    net = Net(
        atoms=atoms,
        bonds=bonds,
        rings=rings,
        ports=new_ports,
        regions=tuple(
            (nm, tuple(remap[a] for a in ords if a in remap))
            for nm, ords in base.regions
        ),
        report=Report(()),
        spec=spec,
        lattice=base.lattice,
        seed3=tuple(map(tuple, pos[keep])),
        seed_kind="mixed",
        sheet_atoms=sheet_atoms,
        sheets=sheets,
    )
    return finish(net)


def _opening_findings(op: Opening, hole: Hole, where: str) -> list[Finding]:
    census = Counter(len(f) for f in op.faces)
    label = f"opening(port={hole.port})"
    out = [
        Finding(
            code="opening.defects",
            severity=Severity.INFO,
            message=(
                f"{label} mouth: {len(op.fused)} fused pairs, defect ring "
                + ", ".join(f"{k}-ring x{v}" for k, v in sorted(census.items()))
            ),
            where=where,
            data=(
                ("pairs", len(op.fused)),
                ("faces", len(op.faces)),
                ("census", dict(sorted(census.items()))),
                ("unbonded_sheet", len(op.unbonded_sheet)),
                ("unbonded_rim", len(op.unbonded_rim)),
                ("removed", len(op.removed)),
            ),
        )
    ]
    if len(op.faces) < len(op.fused):
        out.append(
            Finding(
                code="opening.face.open",
                severity=Severity.WARN,
                message=(
                    f"{label} mouth: {len(op.fused) - len(op.faces)} seam "
                    "faces could not mint a closed arc"
                ),
                where=where,
            )
        )
    return out


__all__ = ["OPENING_RING", "build_openings"]
