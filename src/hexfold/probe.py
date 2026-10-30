"""``hexfold.probe`` — the hex/structure probe resolver (gr522032,
shared-core-layers S2).

The hex half of :func:`precis_diag.probe`: ask what a selector resolves
to at script time, with **no build** — probing reads the bound object's
bookkeeping (atom ordinals, band tuples, ring cells), never composes or
relaxes anything. Bind a resolver with :func:`resolver_for`;
:func:`probe` is the script-facing door — same one-line
``probe <label>: resolves to N <kind>`` shape as ``out.probe`` on the
CAD side and :func:`precis.pcb.local.probe` on the board side, and the
record lands on the calling trace frame so ``view='trace'`` lists it
there.

Selectors — the ``sites:``/``atoms:``/``frame:`` grammar of
``precis-se-regions-help`` (Reto's ruling, gr522032 comment 1):

* ``atoms:<part>[<indices>]`` — ordinals/ranges (``0,3,5-9``,
  inclusive) within the named **part**'s atoms, the se reading of
  ``atoms:<block>[i]``: an instance/region name (a ``Net``'s
  ``regions``) or the whole bound object (``_``/``*``/the object's own
  name).  A band or sub-region (``seam0`` on a bare tube, ``rim``,
  ``t.out``, ``foot``, ``pore0``) is not a part — its atoms index under
  ``sites:`` (``sites:_/foot/s0..s3``, never ``atoms:foot[0-3]``).
* ``sites:<part>/<band>/s<i>..s<j>`` — sites i..j inclusive of a named
  band — ``seam`` in the se spelling is any named atom band here: a
  ``Net`` seam (``foot``) or port (``t.out``), a ``Strip`` rim
  (``rim``/``top``/``top_full``/``pore<i>``), a tube's ``seam<i>`` /
  ``strip<i>`` / ``arc<i>`` / ``wall<i>`` / junction, a ``Tunnel``'s
  ``segment<i>``/``sheet_lo``/``sheet_hi``/``tube``.  Resolves to the
  band's atoms.  ``<part>`` scopes the band lookup: an instance/region
  name (``sites:t/out/s0..s3`` → port ``t.out``) or the whole object
  (``sites:_/seam0/s0..s3``).  ``sites:<part>/<band>`` without the span
  is the whole band's atoms; ``sites:<part>`` alone lists the part's
  **bands** (kind ``bands``, the band keys).  The part is validated on
  every form — an unknown part refuses naming the known ones.
* ``sites:<part>/cells`` — the part's ring **cells** (kind ``cells``:
  the cells whose atoms sit inside the part's, ``(u,v)`` keys on a
  strip, ``r<i>`` elsewhere); ``sites:<part>/<u>,<v>`` (e.g.
  ``sites:st/(2,3)``) is one cell, refused when its atoms reach outside
  the part.  ``cells`` on an object with no ring cells refuses —
  the object has no cells.
* ``frame:<key>[!|/touched|/rest]`` and ``frame:qual=<qual>[…]`` — the
  atoms the named trace frame's subtree made (atoms carry ``maker``
  frame ids; a ``Strip`` resolves through its own whole-block maker).
  ``/touched`` unions the whole subtree's ``touched['atoms']`` ordinal
  ranges — a subtree that recorded only a count (an int, not ranges)
  refuses, since it names no ids.  Needs a live tracer — ``frame:``
  resolves only inside a traced run.

The digest carries ``count`` + ``kind`` + ``ids`` — atom ids are the
hexfold path spellings on a ``Net`` (``t/(0,1,A)``, ``foot/s3``),
labels on a structure scene, ordinals elsewhere; cell/band digests
carry the cell and band keys.  Atom digests also carry ``extent`` (the
atoms' 3D bbox, Å) whenever every resolved position reads — an extra
key, never the digest's identity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from precis_diag import IDS_SAMPLE_CAP, Diag, ProbeError, Status
from precis_diag import probe as _core_probe

from . import _trace

#: The selector spellings this resolver owns.
_PREFIXES = ("sites:", "atoms:", "frame:")

#: ``s<i>..s<j>`` — same inclusive span spelling as the se grammar.
_SPAN_RE = re.compile(r"^s(?P<lo>\d+)\.\.s(?P<hi>\d+)$")
#: ``atoms:<block>[<indices>]`` — the se atoms shape.
_ATOMS_RE = re.compile(r"^(?P<block>[^\[\]]+)\[(?P<idx>[^\[\]]*)\]$")
_INDEX_ITEM_RE = re.compile(r"^(?P<lo>\d+)(?:-(?P<hi>\d+))?$")
#: ``(u,v)`` — a ring-cell address in the band slot.
_CELL_RE = re.compile(r"^\((?P<u>-?\d+),(?P<v>-?\d+)\)$")
#: ``frame:qual=<qual>`` — a frame by call name instead of key.
_QUAL_RE = re.compile(r"^qual=(?P<q>.+)$")
#: ``frame:<key>`` modifier tail: ``!`` the frame alone, ``/touched``
#: its touched ranges, ``/rest`` the subtree minus the frame.
_FRAME_MOD_RE = re.compile(r"^(?P<key>[^!/]+)(?P<mod>!|/touched|/rest)?$")
#: ``assemble`` mints ``pore<N>`` regions for a part's pore rims — a
#: sub-region, i.e. a band for ``atoms:`` purposes, never a part.
_SUB_REGION_RE = re.compile(r"^pore\d+$")

_MAX_ATOMS = 100_000


def _fail(message: str, next_: str) -> ProbeError:
    return ProbeError(
        Diag(
            status=Status.ERROR,
            code="core.probe-unresolved",
            message=message,
            next=next_,
        )
    )


# ---------------------------------------------------------------------------
# the bound object's entity tables (duck-typed — a Net, a Strip, a tube
# bookkeeping object, a structure Scene, a flattened pga scene)
# ---------------------------------------------------------------------------


def _atom_ids(obj: Any) -> tuple[list[str], int]:
    """The object's atom id spellings in ordinal order — hexfold path
    strings on a Net, scene labels on a dict atoms, plain ordinals
    otherwise — plus the atom count."""
    atoms = getattr(obj, "atoms", None)
    if atoms is None and hasattr(obj, "block"):
        block = obj.block
        n = len(getattr(block, "elements", ()) or ())
        return [str(i) for i in range(n)], n
    if isinstance(atoms, dict):
        return [str(la) for la in atoms], len(atoms)
    if atoms is not None:
        out = []
        for i, a in enumerate(atoms):
            path = getattr(a, "path", None)
            out.append(str(path) if path is not None else str(getattr(a, "ord", i)))
        return out, len(atoms)
    coords = getattr(obj, "coords", None)
    if coords is not None:
        n = len(coords)
        return [str(i) for i in range(n)], n
    raise ValueError(f"hex probe: {type(obj).__name__} carries no atoms")


def _positions(obj: Any) -> list[tuple[float, float, float] | None]:
    """Per-ordinal ``(x, y, z)`` or ``None`` where unreadable — the
    digest ``extent``'s source.  Net ``seed3`` (stick seed, may carry
    None), strip ``block.coords``, tube ``coords``, scene ``frac`` through
    the cell's ``frac_to_cart``."""
    if hasattr(obj, "block"):  # Strip
        coords = getattr(obj.block, "coords", None)
        if coords is None:
            return []
        return [(float(p[0]), float(p[1]), float(p[2])) for p in coords]
    atoms = getattr(obj, "atoms", None)
    if isinstance(atoms, dict):
        cell = getattr(obj, "cell", None)
        f2c = getattr(cell, "frac_to_cart", None)
        out: list[tuple[float, float, float] | None] = []
        for a in atoms.values():
            pos = getattr(a, "position", None)
            if pos is None and f2c is not None and hasattr(a, "frac"):
                try:
                    pos = f2c(a.frac)
                except (TypeError, ValueError, IndexError, KeyError):
                    pos = None
            try:
                out.append(
                    None
                    if pos is None
                    else (float(pos[0]), float(pos[1]), float(pos[2]))
                )
            except (TypeError, ValueError, IndexError):
                out.append(None)
        return out
    seed3 = getattr(obj, "seed3", None)
    if seed3 is not None:
        return [
            None if p is None else (float(p[0]), float(p[1]), float(p[2]))
            for p in seed3
        ]
    coords = getattr(obj, "coords", None)
    if coords is not None:
        return [(float(p[0]), float(p[1]), float(p[2])) for p in coords]
    return []


def _makers(obj: Any) -> list[int | None]:
    """Per-ordinal maker frame ids, or ``[]`` when the object's atoms
    carry none (a bare tube's composite coords)."""
    if hasattr(obj, "maker") and getattr(obj, "atoms", None) is None:
        # a Strip: one maker for the whole block — every atom's maker is it
        m = obj.maker
        n = len(getattr(obj.block, "elements", ()) or ())
        return [m] * n
    atoms = getattr(obj, "atoms", None)
    if atoms is None:
        return []
    items = atoms.values() if isinstance(atoms, dict) else atoms
    return [getattr(a, "maker", None) for a in items]


def _bands(obj: Any) -> dict[str, tuple[int, ...]]:
    """The object's named atom bands: name → ordered ordinals.  Net
    ports/seams/regions/sheets/junctions, strip rims and pore rims, tube
    seam/strip/arc/wall/pore/segment bands, tunnel sheets and junctions."""
    out: dict[str, tuple[int, ...]] = {}

    def put(name: Any, ords: Iterable[Any] | None) -> None:
        if name and ords:
            out[str(name)] = tuple(int(o) for o in ords)

    ports = getattr(obj, "ports", None)
    if ports:
        for name, p in ports:
            put(name, getattr(p, "dangling", None) or getattr(p, "atoms", ()))
    for name, ords in getattr(obj, "regions", ()) or ():
        put(name, ords)
    for rec in getattr(obj, "seams", ()) or ():
        put(getattr(rec, "name", None), getattr(rec, "atoms", ()))
    for name, ords in getattr(obj, "sheet_atoms", ()) or ():
        put(name, ords)
    for rec in getattr(obj, "junctions", ()) or ():
        put(getattr(rec, "name", None), getattr(rec, "atoms", ()))
    for name, seg in (getattr(obj, "rims", None) or {}).items():
        ords = getattr(seg, "dangling", None)
        if ords is None:
            da = set(getattr(seg, "dangling_a", ()) or ())
            db = set(getattr(seg, "dangling_b", ()) or ())
            walk = getattr(seg, "atoms", ())
            ords = tuple(o for o in walk if o in da or o in db) or tuple(walk)
        put(name, ords)
    for k, ords in enumerate(getattr(obj, "pore_rims", ()) or ()):
        put(f"pore{k}", ords)
    for family, names in (
        ("seam", "seam_atoms"),
        ("strip", "strip_atoms"),
        ("arc", "arc_atoms"),
        ("wall", "wall_atoms"),
        ("segment", "segment_atoms"),
    ):
        for k, ords in enumerate(getattr(obj, names, ()) or ()):
            put(f"{family}{k}", ords)
    for name in ("sheet_lo", "sheet_hi", "tube_atoms", "pinned"):
        ords = getattr(obj, name, None)
        if ords:
            put("tube" if name == "tube_atoms" else name, ords)
    return out


def _cells(obj: Any) -> list[str]:
    """The object's ring-cell keys — ``(u,v)`` on a strip (the
    ``pore``/``opening`` vocabulary), ``r<i>`` ring index elsewhere."""
    if hasattr(obj, "sites") and hasattr(obj, "periods") and hasattr(obj, "rows"):
        from .sheetnet import _cell_ring

        have = set(obj.sites.values())
        out = []
        for v in range(obj.rows):
            for u in range(-1, obj.periods + 1):
                ring = _cell_ring(obj.periods, u, v)
                if ring and set(ring) <= have:
                    out.append(f"({u},{v})")
        return out
    rings = getattr(obj, "rings", None)
    if rings is None and hasattr(obj, "block"):
        rings = getattr(obj.block, "rings", None)
    if rings is not None:
        return [f"r{i}" for i in range(len(rings))]
    return []


def _cell_atoms(obj: Any, key: str) -> tuple[int, ...] | None:
    """The atom ordinals of one ring cell — a ``(u,v)`` strip cell or a
    ``r<i>`` ring index — or ``None`` when no such cell exists."""
    m = _CELL_RE.match(key)
    if m is not None and hasattr(obj, "sites") and hasattr(obj, "periods"):
        from .sheetnet import _cell_ring

        u, v = int(m["u"]), int(m["v"])
        ring = _cell_ring(obj.periods, u, v)
        have = set(obj.sites.values())
        return tuple(ring) if ring and set(ring) <= have else None
    if key.startswith("r") and key[1:].isdigit():
        rings = getattr(obj, "rings", None)
        if rings is None and hasattr(obj, "block"):
            rings = getattr(obj.block, "rings", None)
        if rings is not None and 0 <= int(key[1:]) < len(rings):
            return tuple(int(o) for o in rings[int(key[1:])])
    return None


def _iter_frames(tr: Any) -> Any:
    stack = [tr.root]
    while stack:
        fr = stack.pop()
        yield fr
        stack.extend(fr.children)


def _frame_subtree_ids(fr: Any) -> set[int]:
    out: set[int] = set()
    stack = [fr]
    while stack:
        f = stack.pop()
        out.add(int(f.id))
        stack.extend(f.children)
    return out


def _frame_subtree_touched(fr: Any) -> tuple[set[int], bool]:
    """The ordinals the subtree's ``touched['atoms']`` ranges cover —
    the se ``touched_ordinals`` reading — plus whether any frame in it
    recorded a bare count (``record`` sums ints into an int) instead of
    ranges.  A count names no ids."""
    out: set[int] = set()
    counted = False
    stack = [fr]
    while stack:
        f = stack.pop()
        stack.extend(f.children)
        touched = (f.touched or {}).get("atoms")
        if isinstance(touched, list):
            for r in touched:
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    out.update(range(int(r[0]), int(r[1]) + 1))
        elif touched is not None:
            counted = True
    return out, counted


class HexProbeResolver:
    """The hex :class:`~precis_diag.ProbeResolver`, bound to one object —
    a ``Net``, a ``Strip``, a tube bookkeeping object, or a structure
    scene.  Pure reads throughout — that is the point of a probe."""

    domain = "hex"

    def __init__(self, target: Any) -> None:
        self._obj = target

    def can_resolve(self, selector: Any) -> bool:
        return isinstance(selector, str) and selector.startswith(_PREFIXES)

    def resolve(self, selector: Any) -> Any:
        if not isinstance(selector, str):
            raise ValueError(f"hex probe: cannot resolve {selector!r}")
        if selector.startswith("atoms:"):
            return self._resolve_atoms(selector)
        if selector.startswith("sites:"):
            return self._resolve_sites(selector)
        if selector.startswith("frame:"):
            return self._resolve_frame(selector)
        raise ValueError(f"hex probe: cannot resolve {selector!r}")

    # -- selectors -----------------------------------------------------------

    def _atom_set(self, part: str) -> tuple[int, ...]:
        """The atoms of the part ``<part>`` names — the whole object on
        ``_``/``*``/its own name, a region/instance name otherwise (the
        se ``atoms:<block>[i]`` reading).  A band or sub-region is not a
        part: its atoms index under ``sites:<part>/<band>``."""
        obj = self._obj
        if part in ("_", "*") or part in self._object_names():
            _, n = _atom_ids(obj)
            return tuple(range(n))
        parts = self._atoms_parts()
        if part in parts:
            return parts[part]
        known = sorted(parts) + sorted(self._object_names())
        raise _fail(
            f"hex probe: no part {part!r} on this {type(obj).__name__}",
            f"'atoms:' indexes a part's atoms — name a part it carries "
            f"({', '.join(known[:8])}{', …' if len(known) > 8 else ''}) "
            "or '_' for the whole object; a band or sub-region's atoms "
            "are 'sites:<part>/<band>'",
        )

    def _atoms_parts(self) -> dict[str, tuple[int, ...]]:
        """Name → atom ordinals for every ``atoms:`` part: the object's
        regions — not its bands or sub-regions, which index under
        ``sites:``.  (A seam's minted atoms carry the seam name as
        their AtomPath instance, so instances are no part test — the
        region table is.)"""
        out: dict[str, tuple[int, ...]] = {}
        for name, ords in getattr(self._obj, "regions", ()) or ():
            if _SUB_REGION_RE.match(str(name)) is None:
                out[str(name)] = tuple(int(o) for o in ords)
        return out

    def _object_names(self) -> tuple[str, ...]:
        out = {type(self._obj).__name__.lower()}
        name = getattr(self._obj, "name", None)
        if isinstance(name, str) and name:
            out.add(name)
        return tuple(sorted(out))

    def _resolve_atoms(self, selector: str) -> dict[str, Any]:
        body = selector.removeprefix("atoms:")
        m = _ATOMS_RE.match(body)
        if m is None or not m["block"].strip():
            raise ValueError(
                f"hex probe: atoms selector is 'atoms:<part>[<indices>]' "
                f"(e.g. atoms:t[0,3,5-9]), got {selector!r}"
            )
        part = m["block"].strip()
        items = [i.strip() for i in m["idx"].split(",")]
        if not items or any(not i for i in items):
            raise ValueError(
                f"hex probe: atoms selector needs at least one index, got {selector!r}"
            )
        idx: list[int] = []
        for i in items:
            mm = _INDEX_ITEM_RE.match(i)
            if mm is None:
                raise ValueError(
                    f"hex probe: bad index {i!r} in {selector!r} — "
                    "indices are ordinals or inclusive a-b ranges"
                )
            lo, hi = int(mm["lo"]), int(mm["hi"] or mm["lo"])
            if hi < lo or hi - lo > _MAX_ATOMS:
                raise ValueError(f"hex probe: bad index range {i!r} in {selector!r}")
            idx.extend(range(lo, hi + 1))
        atoms = self._atom_set(part)
        bad = [i for i in idx if i < 0 or i >= len(atoms)]
        if bad:
            raise _fail(
                f"hex probe: {selector!r} names atom {max(bad)} but "
                f"{part!r} has {len(atoms)} atoms",
                "re-declare the indices against the current object",
            )
        return {
            "kind": "atoms",
            "ords": [atoms[i] for i in sorted(dict.fromkeys(idx))],
        }

    def _resolve_sites(self, selector: str) -> dict[str, Any]:
        body = selector.removeprefix("sites:")
        parts = body.split("/")
        if not (1 <= len(parts) <= 3) or not all(p.strip() for p in parts):
            raise ValueError(
                f"hex probe: sites selector is 'sites:<part>/<band>/s<i>..s<j>', "
                f"got {selector!r}"
            )
        part = parts[0].strip()
        obj = self._obj
        scoped = self._scoped_bands(part)
        if scoped is None:
            known = sorted(self._part_names())
            raise _fail(
                f"hex probe: no part {part!r} on this {type(obj).__name__}",
                f"name a part it carries ({', '.join(known[:8])}"
                f"{', …' if len(known) > 8 else ''}) or '_' for the object",
            )
        if len(parts) == 1:
            keys = sorted(scoped)
            return {"kind": "bands", "keys": keys}
        band = parts[1].strip()
        span = parts[2].strip() if len(parts) == 3 else None
        if band == "cells":
            if not _cells(obj):
                raise _fail(
                    f"hex probe: this {type(obj).__name__} object has no cells",
                    "ring cells come from a strip's (u,v) sites or an "
                    "object's rings — 'sites:<part>' lists the bands instead",
                )
            keys = self._scoped_cells(part, scoped)
            lo, hi = self._span(span, len(keys)) if span else (0, len(keys) - 1)
            return {"kind": "cells", "keys": keys[lo : hi + 1] if keys else []}
        ords = self._band_ords(scoped, part, band)
        if ords is None:
            cells = self._scoped_cell_atoms(part, band, scoped)
            if cells is not None:
                if span:
                    lo, hi = self._span(span, len(cells))
                    return {"kind": "atoms", "ords": list(cells[lo : hi + 1])}
                return {"kind": "cells", "keys": [band]}
            known = sorted(scoped)
            raise _fail(
                f"hex probe: no band {band!r} on {part!r} ({type(obj).__name__})",
                f"bands here look like {', '.join(known[:8]) or '(none)'}"
                f"{', …' if len(known) > 8 else ''}; 'cells' lists ring cells",
            )
        if span:
            lo, hi = self._span(span, len(ords))
            return {"kind": "atoms", "ords": list(ords[lo : hi + 1])}
        return {"kind": "atoms", "ords": list(ords)}

    def _part_names(self) -> set[str]:
        names = set(self._object_names()) | {"_", "*"}
        names.update(
            (getattr(self._obj, "regions", ()) and (n for n, _ in self._obj.regions))
            or ()
        )
        return names

    def _scoped_bands(self, part: str) -> dict[str, tuple[int, ...]] | None:
        """``<part>``'s band table, or ``None`` when the part names
        nothing.  ``_``/``*``/the object names the whole object; an
        instance name scopes to its own bands (port ``t.out`` reachable
        as ``sites:t/out`` and ``sites:_/t.out`` alike)."""
        obj = self._obj
        bands = _bands(obj)
        if part in ("_", "*") or part in self._object_names():
            return bands
        regions = dict(getattr(obj, "regions", ()) or ())
        if part in regions:
            pref = f"{part}."
            scoped = {k[len(pref) :]: v for k, v in bands.items() if k.startswith(pref)}
            scoped[part] = tuple(regions[part])
            return scoped
        if part in bands:
            return {part: bands[part]}
        return None

    def _band_ords(
        self, scoped: dict[str, tuple[int, ...]], part: str, band: str
    ) -> tuple[int, ...] | None:
        if band in scoped:
            return scoped[band]
        qual = f"{part}.{band}"
        return _bands(self._obj).get(qual)

    def _part_atoms(
        self, part: str, scoped: dict[str, tuple[int, ...]]
    ) -> frozenset[int]:
        """The ordinals ``<part>`` covers — every atom on
        ``_``/``*``/the object's name, a region's atoms on a
        region/instance name, the band's on a bare band name."""
        obj = self._obj
        if part in ("_", "*") or part in self._object_names():
            _, n = _atom_ids(obj)
            return frozenset(range(n))
        regions = dict(getattr(obj, "regions", ()) or ())
        if part in regions:
            return frozenset(int(o) for o in regions[part])
        return frozenset(scoped.get(part, ()))

    def _scoped_cells(self, part: str, scoped: dict[str, tuple[int, ...]]) -> list[str]:
        """``<part>``'s ring-cell keys — the cells whose atoms sit
        inside the part's atom set."""
        have = self._part_atoms(part, scoped)
        return [
            key
            for key in _cells(self._obj)
            if (cell := _cell_atoms(self._obj, key)) is not None and set(cell) <= have
        ]

    def _scoped_cell_atoms(
        self, part: str, band: str, scoped: dict[str, tuple[int, ...]]
    ) -> tuple[int, ...] | None:
        cell = _cell_atoms(self._obj, band)
        if cell is None or not set(cell) <= self._part_atoms(part, scoped):
            return None
        return cell

    def _span(self, span: str, n: int) -> tuple[int, int]:
        m = _SPAN_RE.match(span)
        if m is None:
            raise ValueError(f"hex probe: sites span is 's<i>..s<j>', got {span!r}")
        lo, hi = int(m["lo"]), int(m["hi"])
        if lo > hi:
            raise ValueError(f"hex probe: sites range s{lo}..s{hi} runs backwards")
        if hi >= n:
            raise _fail(
                f"hex probe: names s{hi} but the band has {n} site(s)",
                "re-declare the range against the current object",
            )
        return lo, hi

    def _resolve_frame(self, selector: str) -> dict[str, Any]:
        body = selector.removeprefix("frame:")
        m = _FRAME_MOD_RE.match(body)
        if m is None:
            raise ValueError(
                f"hex probe: frame selector is 'frame:<key>[!|/touched|/rest]' "
                f"or 'frame:qual=<qual>[…]', got {selector!r}"
            )
        name, mod = m["key"], m["mod"]
        tr = _trace.current()
        if tr is None:
            raise ValueError(
                "hex probe: no live trace — 'frame:' resolves only inside "
                "a traced run (out.run or trace.run)"
            )
        qm = _QUAL_RE.match(name)
        frames = (
            [f for f in _iter_frames(tr) if f.qual == qm["q"]]
            if qm is not None
            else [f for f in _iter_frames(tr) if f.key == name]
        )
        if not frames:
            raise ValueError(
                f"hex probe: frame {name!r} is not in the live trace — keys "
                "come from the run's trace.json or view='trace'"
            )
        ids: set[int] = set()
        own: set[int] = set()
        touched: set[int] = set()
        counted = False
        for f in frames:
            ids |= _frame_subtree_ids(f)
            own.add(int(f.id))
            sub_touched, sub_counted = _frame_subtree_touched(f)
            touched |= sub_touched
            counted = counted or sub_counted
        if mod == "!":
            ids &= own
        elif mod == "/rest":
            ids -= own
        makers = _makers(self._obj)
        if not makers:
            raise _fail(
                f"hex probe: {type(self._obj).__name__} atoms carry no maker "
                "ids — 'frame:' needs a maker-bearing object",
                "bind the resolver to the Net or scene the run produced",
            )
        if mod == "/touched":
            if counted and not touched:
                raise _fail(
                    f"hex probe: {selector!r} resolves no atoms — "
                    "the subtree's touched atoms are a count",
                    f"frame {name!r} recorded a count, not ids — "
                    "'frame:<key>/touched' resolves touched ordinal "
                    "ranges; 'frame:<key>' still resolves its maker atoms",
                )
            ords = [i for i, mk in enumerate(makers) if mk is not None and i in touched]
        else:
            ords = [
                i for i, mk in enumerate(makers) if mk is not None and int(mk) in ids
            ]
        return {"kind": "atoms", "ords": ords}

    # -- digest --------------------------------------------------------------

    def digest(self, entities: Any) -> dict[str, Any]:
        kind = str(entities.get("kind") or "atoms")
        if kind == "atoms":
            ords = [int(o) for o in entities.get("ords") or ()]
            ids, _n = _atom_ids(self._obj)
            spelled = [ids[o] if 0 <= o < len(ids) else str(o) for o in ords]
            digest: dict[str, Any] = {
                "count": len(ords),
                "kind": "atoms",
                "ids": spelled,
            }
            extent = _extent_of(self._obj, ords)
            if extent is not None:
                digest["extent"] = extent
            if len(spelled) > IDS_SAMPLE_CAP:
                digest["ids"] = spelled[:IDS_SAMPLE_CAP]
                digest["ids_capped"] = len(spelled) - IDS_SAMPLE_CAP
            return digest
        keys = [str(k) for k in entities.get("keys") or ()]
        digest = {"count": len(keys), "kind": kind, "ids": keys}
        if len(keys) > IDS_SAMPLE_CAP:
            digest["ids"] = keys[:IDS_SAMPLE_CAP]
            digest["ids_capped"] = len(keys) - IDS_SAMPLE_CAP
        return digest


def _extent_of(obj: Any, ords: list[int]) -> list[float] | None:
    """The resolved atoms' 3D bbox (object units — Å for every hex
    object), or ``None`` when any resolved position is unreadable."""
    if not ords:
        return None
    positions = _positions(obj)
    if not positions or any(o >= len(positions) for o in ords):
        return None
    pts = [positions[o] for o in ords]
    if any(p is None for p in pts):
        return None
    xs = [p[0] for p in pts if p is not None]
    ys = [p[1] for p in pts if p is not None]
    zs = [p[2] for p in pts if p is not None]
    return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]


def resolver_for(target: Any) -> HexProbeResolver:
    """The probe resolver bound to ``target`` — a ``Net``, a ``Strip``,
    a tube bookkeeping object or a structure scene."""
    return HexProbeResolver(target)


def probe(target: Any, selector: str, *, label: str | None = None) -> dict[str, Any]:
    """Resolve a hex/structure selector against ``target`` — nothing
    runs (gr522032, shared-core-layers S2).

    ``selector`` is the ``sites:``/``atoms:``/``frame:`` grammar (module
    docstring); ``target`` is the object the selector talks about — a
    :class:`hexfold.build.Net`, a :class:`hexfold.sheetnet.Strip`, a tube
    bookkeeping object, or a structure scene.  Prints one
    ``probe <label>: resolves to N <kind>`` line (label defaults to the
    selector) and records ``{"label", "count", "kind", "digest"}`` on
    the calling trace frame, so ``view='trace'`` lists it under the
    frame that asked.  Returns the core mapping
    ``{"count", "digest", "sample"}``.  An unresolvable selector raises
    :class:`precis_diag.ProbeError` carrying the refusal Diag."""
    text = label or str(selector)
    result = _core_probe(selector, label=label, resolver=resolver_for(target))
    kind = result["digest"]["kind"]
    _trace.record_probe(
        {
            "label": str(text),
            "count": result["count"],
            "kind": kind,
            "digest": result["digest"],
        }
    )
    print(f"probe {text}: resolves to {result['count']} {kind}")
    return result


__all__ = ["HexProbeResolver", "probe", "resolver_for"]
