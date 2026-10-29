"""``mol.attach`` — bond a fragment's port to a host site, one fused
:class:`Geom`.

The pose is :func:`precis_geom.assembly.place`'s port-to-port solve: the
host site is synthesised as a port whose position is where the
fragment's attachment atom must land (host atom + free direction × bond
length) and whose direction is the free direction; ``place`` makes the
fragment port's bond vector anti-parallel to it. ``torsion`` then turns
the fragment about the new bond. Everything else here is bookkeeping:
drop the H the new bond replaces, mint fragment labels clear of the
host's, add the bond, and report clashes by the one validator rule
(``precis.structure.validate.OVERLAP_FRACTION``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.structure import elements
from precis.structure.scene import Bond, Scene
from precis.structure.validate import OVERLAP_FRACTION, validate
from precis_geom import assembly as pga
from precis_geom import trace as _trace
from precis_geom.assembly import Geom, Port
from precis_geom.mol import _valence


def _baked(g: Geom, what: str) -> tuple[Scene, dict[str, Port]]:
    """``g``'s merged scene and its ports in that scene's frame (pose
    and placements applied)."""
    if not g.atomic:
        raise BadInput(
            f"mol.attach: {what} has no atomic geometry",
            next="attach joins atomic Geoms (mol.frag / mol.lib / pga.geom)",
        )
    if g.pose is None and not g.placements:
        return g.scene, dict(g.ports)
    ports = (
        pga._transform_ports(g.ports, g.pose) if g.pose is not None else dict(g.ports)
    )
    return g.flatten(), ports


def _tup(v: Any) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


@_trace.traced(name="attach")
def attach(
    host: Any,
    site: Any,
    frag_port: Any,
    *,
    torsion: float = 0.0,
    bond: float | None = None,
) -> Geom:
    """``host`` with the fragment bonded at ``site``.

    ``site`` is a host :class:`Port` (or a ``host.port(name)`` handle) —
    the fragment lands along its ``direction`` — or an atom label with an
    open valence: one bonded H (per unit of bond order) is removed and
    the bond leaves along the H's own direction, else along the free
    direction the neighbours leave (:func:`mol._valence.free_direction`).
    ``frag_port`` is ``frag.port('a1')``. The bond length is the sum of
    covalent radii unless ``bond=`` (Å). ``torsion`` (degrees,
    right-handed about host→fragment) turns the fragment about the new
    bond; the host never moves. Returns one fused ``Geom`` (one scene):
    host and fragment atoms keep their ``maker``, the new bond is made
    by the ``attach`` frame, the unused ports of both carry over, and
    ``findings`` gains one ``attach.clash`` row per new atom pair under
    ``validate.OVERLAP_FRACTION``."""
    if not (isinstance(frag_port, tuple) and len(frag_port) == 2):
        raise BadInput(
            "mol.attach: frag_port must be a port handle, e.g. frag.port('a1')",
            next="mol.attach(host, site, frag.port('a1'))",
        )
    frag_geom, fport = frag_port
    fg_scene, fg_ports = _baked(pga.geom(frag_geom), "the fragment")
    if fport.name in fg_ports:  # the port in the baked frame
        fport = fg_ports[fport.name]
    if fport.atom is None or fport.atom not in fg_scene.atoms:
        raise BadInput(
            f"mol.attach: fragment port {fport.name!r} is not bound to an atom",
            next="use a port from mol.frag / mol.lib (atom= is set)",
        )
    if fport.pos is None or fport.direction is None:
        raise BadInput(
            f"mol.attach: fragment port {fport.name!r} needs pos and direction",
            next="mol.frag(scene=..., ports={'a1': {'atom': '<label>'}})",
        )
    order = float(fport.annotations.get("order", 1.0))

    host_g = pga.geom(host)
    h_scene, h_ports = _baked(host_g, "the host")

    # -- the host site ------------------------------------------------------
    site_obj = site[1] if isinstance(site, tuple) and len(site) == 2 else site
    consumed: str | None = None
    removed: list[str] = []
    if isinstance(site_obj, Port):
        label = site_obj.atom
        if label is None or label not in h_scene.atoms:
            raise BadInput(
                f"mol.attach: host port {site_obj.name!r} is not bound to a host atom",
                next="bind it: ports={'a1': {'atom': '<label>'}}",
            )
        direction = (
            _valence.free_direction(h_scene, label, prefer_h=False)
            if site_obj.direction is None
            else np.asarray(site_obj.direction, dtype=float)
        )
        consumed = site_obj.name
        _valence.require_open_valence(h_scene, label, order, 0)
    elif isinstance(site_obj, str):
        label = site_obj
        if label not in h_scene.atoms:
            raise BadInput(
                f"mol.attach: no host atom {label!r}",
                options=list(h_scene.atoms)[:20],
                next="pass a host atom label or a host Port",
            )
        hs = _valence.bonded_hydrogens(h_scene, label)
        removed = hs[: max(1, round(order))] if hs else []
        _valence.require_open_valence(h_scene, label, order, len(removed))
        direction = _valence.free_direction(h_scene, label, prefer_h=True)
    else:
        raise BadInput(
            f"mol.attach: site must be a host Port or an atom label, got "
            f"{type(site_obj).__name__}",
            next="host.port('a1') or an atom label like 'aC1'",
        )
    d = direction / float(np.linalg.norm(direction))

    h_el = h_scene.atoms[label].element
    f_el = fg_scene.atoms[fport.atom].element
    length = (
        float(bond)
        if bond is not None
        else elements.covalent_radius(h_el) + elements.covalent_radius(f_el)
    )
    if not length > 0.0:
        raise BadInput(
            f"mol.attach: bond length must be > 0 Å, got {bond!r}",
            next="omit bond= for the covalent-radii sum",
        )

    # -- the pose: place's solve, then the torsion ---------------------------
    host_pos = _valence.cart(h_scene, label)
    anchor = host_pos + d * length
    placed = pga.place(
        Geom(scene=fg_scene, ports=fg_ports, meta=dict(frag_geom.meta)),
        at=(
            Geom(scene=h_scene),
            Port(name="site", pos=_tup(anchor), direction=_tup(d), atom=label),
        ),
        align=(frag_geom, fport),
    )
    T = placed.pose
    if T is None:  # pragma: no cover - place always sets it
        raise BadInput("mol.attach: place returned no pose")
    Rt = pga._rot_axis(d, math.radians(float(torsion)))

    def pose(x: np.ndarray) -> np.ndarray:
        return Rt @ (T.apply(x) - anchor) + anchor

    def pose_dir(v: np.ndarray) -> np.ndarray:
        return Rt @ T.apply_dir(v)

    # -- merge ----------------------------------------------------------------
    maker = pga._leaf()
    keep_cell = any(h_scene.cell.pbc)
    h_labels = [la for la in h_scene.atoms if la not in removed]
    f_labels = list(fg_scene.atoms)
    carts: dict[str, np.ndarray] = {la: _valence.cart(h_scene, la) for la in h_labels}
    label_hi = dict(h_scene.label_hi)
    for el in {a.element for a in h_scene.atoms.values()}:
        label_hi[el] = max(
            label_hi.get(el, 0), int(h_scene.next_label(el)[1 + len(el) :]) - 1
        )
    f_carts = {la: pose(_valence.cart(fg_scene, la)) for la in f_labels}
    cell = (
        h_scene.cell
        if keep_cell
        else pga._bounds_cell(np.asarray(list(carts.values()) + list(f_carts.values())))
    )
    out = Scene(cell=cell, label_hi=label_hi, measures=list(h_scene.measures))
    for la in h_labels:
        out.atoms[la] = replace(h_scene.atoms[la], frac=cell.cart_to_frac(carts[la]))
    new_label: dict[str, str] = {}
    for la in f_labels:
        a = fg_scene.atoms[la]
        nl = out.next_label(a.element)
        new_label[la] = nl
        out.atoms[nl] = replace(
            a,
            label=nl,
            frac=cell.cart_to_frac(f_carts[la]),
            maker=a.maker if a.maker is not None else maker,
        )
    rm = set(removed)
    out.bonds = [replace(b) for b in h_scene.bonds if b.i not in rm and b.j not in rm]
    for b in fg_scene.bonds:
        if b.i in new_label and b.j in new_label:
            out.bonds.append(
                replace(
                    b,
                    i=new_label[b.i],
                    j=new_label[b.j],
                    maker=b.maker if b.maker is not None else maker,
                )
            )
    out.bonds.append(
        Bond(
            i=label,
            j=new_label[fport.atom],
            order=order,
            kind="pairwise",
            provenance="declared",
            maker=maker,
        )
    )

    # -- ports ------------------------------------------------------------------
    ports: dict[str, Port] = {
        n: p
        for n, p in h_ports.items()
        if n != consumed and (p.atom is None or p.atom not in rm)
    }
    prefix = frag_geom.name or "frag"
    for n, p in fg_ports.items():
        if n == fport.name:
            continue
        key = n if n not in ports else f"{prefix}.{n}"
        ports[key] = replace(
            p,
            name=key,
            pos=_tup(pose(np.asarray(p.pos, dtype=float))) if p.pos else None,
            direction=(
                _tup(pose_dir(np.asarray(p.direction, dtype=float)))
                if p.direction
                else None
            ),
            atom=new_label.get(p.atom) if p.atom else None,
        )

    # -- clash rows: the validator's overlap rule, new atoms only -----------------
    fresh = set(new_label.values())
    clashes = tuple(
        f"attach.clash: {i.atoms[0]}–{i.atoms[1]} {i.measured:.2f} Å < "
        f"{i.expected:.2f} Å ({OVERLAP_FRACTION:g} × covalent radii)"
        for i in validate(out)
        if i.rule == "atom_overlap" and fresh & set(i.atoms)
    )
    pga._record(atoms=len(f_labels), bonds=len(fg_scene.bonds) + 1)
    _trace.record(touched={"atoms": 1 + len(removed)})
    return Geom(
        scene=out,
        ports=ports,
        meta=dict(host_g.meta),
        findings=(*host_g.findings, *frag_geom.findings, *clashes),
    )
