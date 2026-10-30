"""``mol.decorate`` and ``mol.cap`` — a group at every selected site.

``decorate`` is one ``mol.attach`` per site (:mod:`precis_geom.mol.selector`
picks them). The torsion about each new bond is scanned — 12 turns of 30°,
the one with the largest minimum non-bonded distance wins, exact ties
broken by ``seed`` — and a site whose best torsion still overlaps under
``validate.OVERLAP_FRACTION`` (the one clash rule) is skipped with a
``decorate.site_skipped`` finding; a site with no open valence (and no H
to replace) gets ``decorate.valence``. The counts ride the trace frame's
``made``.

``cap`` is ``decorate(sites='open', group='H')`` — except on a Geom built
from a hexfold Net, where it is ``terminate_open_edges``, the function
the ``terminate`` op places its H with.
"""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.structure import elements
from precis.structure.scene import Atom, Bond
from precis.structure.validate import OVERLAP_FRACTION
from precis_geom import assembly as pga
from precis_geom import trace as _trace
from precis_geom.assembly import Geom, Port
from precis_geom.mol import _valence, attachment
from precis_geom.mol import selector as _sites
from precis_geom.mol.library import NAMES as _LIB_NAMES
from precis_geom.mol.library import lib

#: Torsions tried per site, degrees.
_SCAN = tuple(30.0 * k for k in range(12))
#: C–H length of a placed cap — hexfold's ``sigma_CH_A``.
_CH_A = 1.09
#: Torsion scores within this are one tie (broken by ``seed``).
_TIE_A = 1e-9
#: ``cap`` places one H per open atom per pass; an atom has at most four.
_MAX_CAP_PASSES = 4


def _h_group() -> Geom:
    """A bare H atom with one port: what ``group='H'`` attaches."""
    scene = pga._scene_from_parts(["H"], np.zeros((1, 3)), [])
    label = next(iter(scene.atoms))
    port = Port(
        name="a1",
        pos=(0.0, 0.0, 0.0),
        direction=(0.0, 0.0, 1.0),
        atom=label,
        annotations={"order": 1.0},
    )
    return Geom(scene=scene, ports={"a1": port}, meta={"units": "A", "name": "H"})


def _resolve_group(group: Any) -> tuple[Geom, str, str]:
    """``(geom, port name, display name)`` for a group spec: ``'H'``, a
    library name (``'COOH'``), a SMILES with ``[*:1]``, or a Geom."""
    if isinstance(group, str):
        if group == "H":
            return _h_group(), "a1", "H"
        key = {k.lower(): k for k in _LIB_NAMES}.get(group.lower())
        if key is not None and "*" not in group:
            g = lib(key)
        elif "*" in group:
            from precis_geom.mol.fragment import frag

            g = frag(group)
        else:
            raise BadInput(
                f"mol.decorate: unknown group {group!r}",
                options=["H", *_LIB_NAMES],
                next="a library name, 'H', a SMILES with an attachment "
                "point ('[*:1]C(=O)O') or a Geom from mol.frag / mol.lib",
            )
    elif isinstance(group, Geom):
        g = group
    else:
        raise BadInput(
            f"mol.decorate: group must be 'H', a library name, a SMILES or a "
            f"Geom, got {type(group).__name__}",
            options=["H", *_LIB_NAMES],
            next="mol.decorate(g, sites='open', group='COOH')",
        )
    if not g.ports:
        raise BadInput(
            f"mol.decorate: group {g.name or 'geom'!r} has no port to bond through",
            next="use a fragment with an attachment point: mol.frag('[*:1]...')",
        )
    pname = "a1" if "a1" in g.ports else sorted(g.ports)[0]
    return g, pname, str(g.name or group)


def _tie_pick(cands: list[float], seed: int, index: int) -> float:
    return random.Random(f"{seed}:{index}").choice(cands)


def _scan(
    plan: attachment._Plan,
    torsions: tuple[float, ...],
    seed: int,
    index: int,
) -> tuple[float, float, bool]:
    """``(torsion, min non-bonded distance, overlaps)`` of the best
    candidate. Non-bonded: every fragment/host pair except the new bond's
    own 1-2 and 1-3 pairs; ``overlaps`` is the validator's rule
    (distance under ``OVERLAP_FRACTION`` × covalent radii) on those
    pairs."""
    from scipy.spatial import cKDTree

    hs = plan.h_scene
    removed = set(plan.removed)
    host_labels = [la for la in hs.atoms if la not in removed]
    host_pts = np.asarray([_valence.cart(hs, la) for la in host_labels], dtype=float)
    host_el = [hs.atoms[la].element for la in host_labels]
    tree = cKDTree(host_pts)
    site = plan.label
    near_site = {site} | {
        o for o, _od, _v in _valence.bond_vectors(hs, site) if o not in removed
    }
    f_att = str(plan.fport.atom)
    f_near = {f_att} | {
        o
        for o, _od, _v in _valence.bond_vectors(plan.fg_scene, f_att)
        if o in plan.fg_scene.atoms
    }
    rcov = elements.covalent_radius
    reach = 2.0 * max(rcov(e) for e in set(host_el) | {"C"}) + 3.0
    scored: list[tuple[float, float, bool]] = []
    for t in torsions:
        carts = plan.f_carts(t)
        best, hit = np.inf, False
        for fl, p in carts.items():
            f_el = plan.fg_scene.atoms[fl].element
            for k in tree.query_ball_point(p, reach):
                hl = host_labels[k]
                if (fl == f_att and hl in near_site) or (fl in f_near and hl == site):
                    continue
                d = float(np.linalg.norm(host_pts[k] - p))
                best = min(best, d)
                if d < OVERLAP_FRACTION * (rcov(f_el) + rcov(host_el[k])):
                    hit = True
        scored.append((t, float(best), hit))
    top = max(s[1] for s in scored)
    tied = [s for s in scored if s[1] >= top - _TIE_A]
    pick = _tie_pick([s[0] for s in tied], seed, index)
    chosen = next(s for s in tied if s[0] == pick)
    return chosen


@_trace.traced(name="decorate")
def decorate(
    geom: Any,
    sites: Any,
    group: Any = None,
    *,
    pattern: Any = None,
    seed: int = 0,
    torsion: Any = "scan",
) -> Geom:
    """``geom`` with ``group`` bonded at every one of ``sites``.

    ``sites`` is a selector string (:func:`mol.sites`: ``'open & face:out
    & every:3'``) or a list of :class:`MolSite`. ``group`` is ``'H'``, a
    library name, a SMILES with ``[*:1]`` or a Geom; ``pattern=`` (a
    sequence of groups) replaces it and cycles across the sites. A site
    with an open valence takes the group on it; a site that is only H-capped
    swaps one H for it; ``group='H'`` on a capped site is saturated.

    ``torsion='scan'`` tries 12 turns about each new bond and keeps the
    one with the largest minimum non-bonded distance (``seed`` breaks exact
    ties); a number fixes it (degrees). Findings on the result:
    ``decorate.site_skipped`` (the best torsion still overlaps under
    ``validate.OVERLAP_FRACTION`` — nothing placed there) and
    ``decorate.valence`` (the site is saturated). Their counts, and the
    groups and atoms placed, are the frame's ``made``."""
    if (group is None) == (pattern is None):
        raise BadInput(
            "mol.decorate takes exactly one of group= and pattern=",
            next="group='COOH', or pattern=['OH', 'CH3'] to cycle",
        )
    specs = [group] if pattern is None else list(pattern)
    if not specs:
        raise BadInput("mol.decorate: pattern is empty", next="pattern=['OH', 'CH3']")
    groups = [_resolve_group(s) for s in specs]
    if torsion == "scan":
        scan: tuple[float, ...] = _SCAN
    else:
        try:
            scan = (float(torsion),)
        except (TypeError, ValueError):
            raise BadInput(
                f"mol.decorate: torsion must be 'scan' or degrees, got {torsion!r}",
                options=["scan"],
                next="torsion='scan' or torsion=60.0",
            ) from None

    g = _sites.bake(pga.geom(geom))
    rows = _sites.sites(g, sites) if isinstance(sites, str) else list(sites)
    cur = g
    findings: list[str] = []
    placed = skipped = saturated = n_atoms = 0
    for k, row in enumerate(rows):
        grp, pname, gname = groups[k % len(groups)]
        scene = cur.scene
        if row.atom not in scene.atoms:  # a prior site's swap removed it
            continue
        nets = _sites.hyb_map(cur)
        hyb = _sites.hyb_of(scene, row.atom, nets)
        free = _valence.open_count(scene, row.atom, hyb=hyb)
        has_h = bool(_valence.bonded_hydrogens(scene, row.atom))
        if free >= 1:
            site: Any = Port(
                name="site",
                atom=row.atom,
                direction=attachment._tup(
                    _valence.free_direction(scene, row.atom, prefer_h=False, hyb=hyb)
                ),
            )
        elif has_h and gname != "H":
            site = row.atom  # swap one H for the group
        else:
            saturated += 1
            findings.append(
                f"decorate.valence: {row.atom} ({row.element}) has no open "
                "valence" + (" and its H is already the group" if has_h else "")
            )
            continue
        is_h_on_c = gname == "H" and scene.atoms[row.atom].element == "C"
        plan = attachment._plan(
            cur, site, (grp, grp.ports[pname]), bond=_CH_A if is_h_on_c else None
        )
        # a single atom has nothing to turn
        t, best, hit = _scan(plan, scan if len(plan.f_labels) > 1 else (0.0,), seed, k)
        if hit:
            skipped += 1
            findings.append(
                f"decorate.site_skipped: {row.atom} ({row.ring_class}, "
                f"face {row.face}): best torsion {t:g}° still leaves a "
                f"non-bonded pair at {best:.2f} Å, under {OVERLAP_FRACTION:g} × "
                "covalent radii"
            )
            continue
        cur = attachment.attach_planned(plan, t)
        placed += 1
        n_atoms += len(plan.f_labels)
    out = Geom(
        scene=cur.scene,
        ports=cur.ports,
        meta=dict(cur.meta),
        findings=(*g.findings, *findings),
    )
    _trace.record(
        made={
            "groups": placed,
            "atoms": n_atoms,
            "decorate.site_skipped": skipped,
            "decorate.valence": saturated,
        }
    )
    return out


def _untouched_hosts(g: Geom, ref: Any) -> frozenset[int]:
    """Net ordinals whose scene atom still has exactly the Net's bonds:
    nothing decorated or capped there yet. A site a group (or an H) was
    bonded to has consumed that open valence — ``terminate_open_edges``
    reads the Net's degree alone and would cap it again."""
    _used, count = _valence.bond_tables(g.scene)
    degree = [0] * len(ref.net.atoms)
    for i, j, *_ in ref.net.bonds:
        degree[i] += 1
        degree[j] += 1
    labels = list(ref.scene.atoms)  # ordinal → label
    return frozenset(o for o, la in enumerate(labels) if count.get(la, 0) == degree[o])


def _cap_net(g: Geom, ref: Any) -> Geom:
    """The Net path: ``hexfold_spec.terminate_open_edges`` places the H,
    exactly as the ``terminate`` op does — on the hosts nothing has been
    bonded to since the Net was exported (all of them, on a pristine
    Geom)."""
    from precis_se.atomic.generators.hexfold_spec import terminate_open_edges

    hosts, positions = terminate_open_edges(
        ref.net,
        ref.xyz,
        "H",
        eligible_hosts=None if g.scene is ref.scene else _untouched_hosts(g, ref),
    )
    scene = copy.deepcopy(g.scene)
    labels = list(ref.scene.atoms)  # ordinal → label (Net ordinals are scene order)
    maker = pga._leaf()
    for host, p in zip(hosts, positions):
        nl = scene.next_label("H")
        scene.atoms[nl] = Atom(
            label=nl,
            element="H",
            frac=scene.cell.cart_to_frac(np.asarray(p, dtype=float)),
            maker=maker,
        )
        scene.bonds.append(
            Bond(
                i=labels[host],
                j=nl,
                order=1.0,
                kind="pairwise",
                provenance="declared",
                maker=maker,
            )
        )
    pga._record(atoms=len(hosts), bonds=len(hosts))
    return Geom(
        scene=scene,
        ports=dict(g.ports),
        meta={**g.meta, "capped": len(hosts)},
        findings=g.findings,
    )


@_trace.traced(name="cap")
def cap(geom: Any, group: Any = "H") -> Geom:
    """``geom`` with every open valence capped by ``group`` (default H).

    On a Geom built from a hexfold Net (``pga.geom(net, coords=...)``)
    with ``group='H'`` this is ``hexfold_spec.terminate_open_edges`` — the
    very function the ``terminate`` op uses, so the two agree by
    construction. After a ``decorate`` the Net still describes the
    unreached edge atoms: they take the Net path, the sites the
    decoration bonded to are left out, and ``decorate(sites='open')``
    finishes what is left (an atom a group only half-filled). Any other
    Geom is ``decorate(sites='open', group=group)``, repeated until no
    open valence is left (one group per open atom per pass)."""
    g = _sites.bake(pga.geom(geom))
    ref = _sites.net_ref(g)
    if ref is not None and group == "H":
        out = _cap_net(g, ref)
        if g.scene is ref.scene:  # pristine: the Net path is the whole cap
            return out
        g = out
    out = g
    for _ in range(_MAX_CAP_PASSES):
        rows = _sites.sites(out, "open")
        if not rows:
            break
        before = len(out.scene.atoms)
        out = decorate(out, rows, group)
        if len(out.scene.atoms) == before:  # every open site skipped: stuck
            break
    # a site skipped on one pass is skipped again on the next: say it once
    return replace(out, findings=tuple(dict.fromkeys(out.findings)))
