"""``precis_geom.probe`` — the CAD probe resolver (gr522026,
shared-core-layers S1c).

The domain half of :func:`precis_diag.probe` for workspace scripts:
``out.run(...) as o: o.probe(selector, label=...)`` resolves, at script
time and without running anything,

* a ``frame:<key>`` selector against the *live* tracer — the entities
  are the frame's own ``made`` counts, so the digest answers "what did
  that frame place" (keys are stable across reruns of an unchanged
  script, so a key copied from the last run's ``trace.json`` or from
  ``view='trace'`` resolves here);
* a pg object carrying ``.atoms`` (a structure scene, a flattened
  ``pga.Geom``, a hexfold ``Net``/``Strip``) — the entities are its
  atoms. Positions are read duck-typed (``.position`` or a
  ``"position"`` key); with them the digest carries a ``bbox`` in the
  object's own units (Å for scenes), without them the atom ordinals or
  labels as ``ids`` — capped at
  :data:`~precis_diag.IDS_SAMPLE_CAP` with an ``ids_capped`` marker,
  since a probe record rides the trace and must stay bounded.

Importing this module registers the resolver (domain ``cad``) with
:func:`precis_diag.register_probe`; :mod:`precis_geom.out` imports it,
so every ``out.run`` script gets dispatch without a boilerplate import.
"""

from __future__ import annotations

from typing import Any

from precis_diag import IDS_SAMPLE_CAP, register_probe


def _iter_frames(tr: Any) -> Any:
    """Every frame of the live trace tree, root first."""
    stack = [tr.root]
    while stack:
        fr = stack.pop()
        yield fr
        stack.extend(fr.children)


class CadProbeResolver:
    """The CAD :class:`~precis_diag.ProbeResolver`: ``frame:<key>``
    strings and pg objects with ``.atoms``."""

    domain = "cad"

    def can_resolve(self, selector: Any) -> bool:
        if isinstance(selector, str):
            return selector.startswith("frame:")
        return hasattr(selector, "atoms")

    def resolve(self, selector: Any) -> Any:
        if isinstance(selector, str):
            return self._resolve_frame(selector)
        if hasattr(selector, "atoms"):
            return selector
        raise ValueError(f"cad probe: cannot resolve {selector!r}")

    def digest(self, entities: Any) -> dict[str, Any]:
        if isinstance(entities, dict):
            # A traced frame's made counts: {entity_type: int}.
            ids = [f"{k}:{v}" for k, v in sorted(entities.items())]
            return {
                "count": sum(int(v) for v in entities.values()),
                "kind": "entities",
                "ids": ids,
            }
        atoms = entities.atoms
        items = list(atoms.items()) if isinstance(atoms, dict) else None
        if items is not None:
            ids = [str(label) for label, _atom in items]
        else:
            ids = [str(getattr(a, "ord", i)) for i, a in enumerate(atoms)]
        positions = _positions_of(entities)
        if positions and all(p is not None for p in positions):
            pts = [p for p in positions if p is not None]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            zs = [p[2] for p in pts]
            return {
                "count": len(pts),
                "kind": "atoms",
                "bbox": [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)],
            }
        digest: dict[str, Any] = {"count": len(ids), "kind": "atoms", "ids": ids}
        if len(ids) > IDS_SAMPLE_CAP:
            digest["ids"] = ids[:IDS_SAMPLE_CAP]
            digest["ids_capped"] = len(ids) - IDS_SAMPLE_CAP
        return digest

    def _resolve_frame(self, selector: str) -> dict[str, int]:
        key = selector.removeprefix("frame:")
        from precis_geom import trace as _trace

        tr = _trace.current()
        if tr is None:
            raise ValueError(
                f"cad probe: no live trace — 'frame:{key}' resolves only inside out.run"
            )
        for fr in _iter_frames(tr):
            if fr.key == key:
                return dict(fr.made)
        raise ValueError(
            f"cad probe: frame {key!r} is not in the live trace — keys "
            "come from the run's trace.json or view='trace'"
        )


def _positions_of(obj: Any) -> list[tuple[float, float, float] | None] | None:
    """Per-atom ``(x, y, z)`` positions duck-typed across the pg object
    models: atoms carrying ``.position``/a ``"position"`` key (external
    models), or a ``.cell.frac_to_cart`` with atoms in fractional coords
    (structure scenes). ``None`` per atom when its position cannot be
    read; the whole list ``None`` when the object carries no atoms."""
    atoms = getattr(obj, "atoms", None)
    if atoms is None:
        return None
    items = list(atoms.values()) if isinstance(atoms, dict) else list(atoms)
    frac_to_cart = getattr(getattr(obj, "cell", None), "frac_to_cart", None)
    out: list[tuple[float, float, float] | None] = []
    for atom in items:
        pos = _position_of(atom)
        if pos is None and frac_to_cart is not None and hasattr(atom, "frac"):
            try:
                cart = frac_to_cart(atom.frac)
                pos = (float(cart[0]), float(cart[1]), float(cart[2]))
            except (TypeError, ValueError, IndexError, KeyError):
                pos = None
        out.append(pos)
    return out


def _position_of(atom: Any) -> tuple[float, float, float] | None:
    """An atom's ``(x, y, z)`` duck-typed — ``.position`` or a mapping
    key — or ``None`` when the atom carries no position of its own."""
    pos = getattr(atom, "position", None)
    if pos is None and isinstance(atom, dict):
        pos = atom.get("position")
    if pos is None:
        return None
    try:
        return (float(pos[0]), float(pos[1]), float(pos[2]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


register_probe(CadProbeResolver())

__all__ = ["CadProbeResolver"]
