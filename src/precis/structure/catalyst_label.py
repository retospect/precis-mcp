"""Human-readable catalyst names from a structure's recorded construction
(gr553416).

:func:`catalyst_label` renders ``'Pd(111) + Ta (subsurface) + vacancy
(surface, co-located)'`` — the readable catalyst name pathway titles and
cards carry — off what was actually *recorded*: the recipe ops
(``design_revisions``: ``slab`` → host element + fcc(111) facet;
``set_element`` substitutions with their lattice layer; ``add_atom_site``
adatoms with their named site; ``add_atom``'s below-surface intent markers
(gr549760); ``vacancy``; ``add_adsorbate`` groups) and the ``meta.params``
param-space stamp (:func:`precis.quest.compute.params_from_spec`). The
structure's title string is never parsed — ``None`` when no construction
is recorded, so callers keep the title-derived name.

Two recorded facts do the quiet lifting:

- **Slab layers by label arithmetic.** ``slab`` builds fcc(111) in ASE's
  z-ascending atom order and labels mint ``a<El><n>`` sequentially (never
  recycled), so ``aPd35`` of a ``[3, 3, 4]`` slab IS a top-layer atom — a
  ``vacancy`` or a by-label ``set_element`` gets its layer word without
  the geometry. Layer words count from the surface: ``surface`` (depth
  1), ``subsurface`` (2), ``subsurface, layer <depth>`` (deeper). A mint
  the arithmetic can't account for (``from_smiles``,
  ``import_fragment``) turns layer words off for the rest of the recipe —
  an honest bare ``vacancy`` over a wrong layer.
- **Co-location by ``near``.** A removed atom label referenced as a
  ``site.near`` target (or a ``near``/``colocated`` extra on the vacancy
  op itself — the engine ignores it, the record keeps it) renders
  ``co-located`` on the vacancy.

Pure stdlib on purpose: importable from ``precis_pathway.persist``'s
cheap-import surface and the structure card alike.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

#: Atom-label shape (``a<El><n>`` — minted in op order, never recycled).
_LABEL_RE = re.compile(r"^a([A-Z][a-z]?)(\d+)$")

#: Simple hill-formula parse for an ``add_adsorbate`` species' per-element
#: label-mint bookkeeping (``'H2O'`` → ``{'H': 2, 'O': 1}``).
_FORMULA_RE = re.compile(r"([A-Z][a-z]?)(\d*)")

#: Elements ``params_from_spec`` counts as co-adsorbates rather than
#: dopants — mirrored from ``precis.quest.compute._COADSORBATE_SPECIES``
#: (the quest package layers *above* this one, so the closed set is
#: duplicated here with its source named, not imported).
_ADSORBATE_ELEMENTS = frozenset({"H", "O", "OH", "H2O", "N", "NH", "NH2", "NH3"})

#: Ops whose label mints the arithmetic accounts for by counting fields
#: (``ring``'s ``n``) or by species formula (``add_adsorbate``). Anything
#: that mints an uncountable set — ``from_smiles``, ``import_fragment`` —
#: flips label arithmetic to untrusted.
_UNCOUNTABLE_MINTS = frozenset({"from_smiles", "import_fragment"})

#: Extra keys a ``vacancy`` op may carry declaring the removal co-located
#: with another feature — engine-ignored, but recorded verbatim in
#: ``design_revisions``.
_COLOCATED_KEYS = ("colocated", "co_located", "co-located")

#: ``set_element``'s by-name lattice pick (``_SUBST_LAYER`` in
#: :mod:`precis.structure.ops`): the layer a by-site substitution lands
#: in, as depth from the top.
_SUBST_DEPTH = {"top": 1, "subsurface": 2}


def catalyst_label(source: Any) -> str | None:
    """``'Pd(111) + Ta (subsurface) + vacancy (surface, co-located)'`` —
    the readable catalyst name — from a structure's recorded construction.

    ``source`` may be a bare ops list, a spec dict (``{"cell": …,
    "ops": […]}``), a structure's ``meta`` dict (``params`` /
    ``site_intent`` / ``composition`` / ``generated.spec``), or a
    ``{"ops": […], "meta": {…}}`` bundle — whatever the call site already
    holds. Returns ``None`` when no construction is recorded so the
    caller keeps the title-derived name.
    """
    ops, meta = _split_source(source)
    if ops:
        label = _from_ops(ops, meta)
        if label is not None:
            return label
    return _from_meta(meta)


def _split_source(source: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(ops, meta)`` out of whatever was handed over."""
    if source is None:
        return [], {}
    if isinstance(source, list):
        return [o for o in source if isinstance(o, dict)], {}
    if not isinstance(source, dict):
        return [], {}
    meta = source.get("meta")
    meta = meta if isinstance(meta, dict) else source
    ops = source.get("ops")
    if not isinstance(ops, list):
        ops = _generated_ops(meta)
    return [o for o in ops if isinstance(o, dict)], meta


def _generated_ops(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Ops recorded inside a ``meta.generated`` build record — a spec
    generator keeps its source spec there (a dict, or a JSON string)."""
    rec = meta.get("generated")
    spec = rec.get("spec") if isinstance(rec, dict) else None
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            return []
    ops = spec.get("ops") if isinstance(spec, dict) else None
    return [o for o in ops if isinstance(o, dict)] if isinstance(ops, list) else []


def _element_of(op: dict[str, Any]) -> str | None:
    el = op.get("element")
    return el.strip() if isinstance(el, str) and el.strip() else None


def _majority(composition: Any) -> str | None:
    """The plurality element of ``meta.composition`` — deterministic on
    ties (``sorted`` first, then ``max`` by count)."""
    if not isinstance(composition, dict) or not composition:
        return None
    return max(sorted(composition), key=lambda e: composition[e])


def _formula_counts(species: str) -> dict[str, int] | None:
    """``'H2O' → {'H': 2, 'O': 1}`` for label-mint bookkeeping; ``None``
    when the species parses to nothing (a mint the arithmetic cannot
    account for → untrusted)."""
    counts = {el: int(n) if n else 1 for el, n in _FORMULA_RE.findall(species)}
    return counts or None


def _near_label(op: dict[str, Any]) -> str | None:
    """The atom label an op names as its placement neighbour — ``near``
    inside the ``site`` object (``set_element`` / ``add_atom_site`` /
    ``add_adsorbate`` / ``move_group``) or an op-level ``near``. Only
    label-shaped values count: an element name means 'any atom of it',
    never one recorded neighbour."""
    site = op.get("site")
    near = site.get("near") if isinstance(site, dict) else None
    if near is None:
        near = op.get("near")
    return near if isinstance(near, str) and _LABEL_RE.match(near) else None


def _depth_words(depth: int | None) -> str:
    """Layer words counted from the surface: ``surface`` (1),
    ``subsurface`` (2), ``subsurface, layer <depth>`` (deeper)."""
    if depth == 1:
        return "surface"
    if depth == 2:
        return "subsurface"
    if depth is not None and depth >= 3:
        return f"subsurface, layer {depth}"
    return ""


def _subst_words(depth: int | None) -> str:
    """A substitution's site words: the surface layer is the stated case
    (``surface substitution``); a deeper one reads by its depth
    (``subsurface`` / ``subsurface, layer <depth>``)."""
    if depth == 1:
        return "surface substitution"
    if depth is None:
        return "substitution"
    return _depth_words(depth)


def _slab_geometry(
    slab: dict[str, Any], meta: dict[str, Any]
) -> tuple[float, float, float] | None:
    """``(spacing, z_top, c)`` of the slab's layer stack — the positions a
    below-surface marker is measured against. Preferred source: the
    recorded ``meta.lattice`` (the saved cell is construction too). Else
    the slab op's own numbers — which need an explicit ``a`` (the ASE
    reference default lives outside the record). ``vacuum``'s 10.0 Å
    default IS recorded in the op contract."""
    nz = slab["nz"]
    lat = meta.get("lattice")
    if isinstance(lat, list) and len(lat) == 3:
        try:
            a1 = math.sqrt(sum(float(x) ** 2 for x in lat[0]))
            a2 = math.sqrt(sum(float(x) ** 2 for x in lat[1]))
            c = float(lat[2][2])
            lateral = (a1 / slab["nx"] + a2 / slab["ny"]) / 2
            spacing = lateral * math.sqrt(2) / math.sqrt(3)
            thickness = (nz - 1) * spacing
            return spacing, (c + thickness) / 2, c
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            pass
    a = slab.get("a")
    if a is None:
        return None
    vacuum = slab["vacuum"]
    spacing = a / math.sqrt(3)
    thickness = (nz - 1) * spacing
    return spacing, vacuum + thickness, 2 * vacuum + thickness


def _marker_depth(
    op: dict[str, Any], slab: dict[str, Any] | None, meta: dict[str, Any]
) -> int | None:
    """The layer an ``add_atom`` below-surface marker atom actually sits
    in — from its recorded ``cart``/``frac`` z measured against the slab's
    layer stack (``_slab_geometry``). ``None`` when the record doesn't
    carry the geometry — the marker then reads by its declared intent
    only (``subsurface`` / ``interstitial``, no layer number)."""
    if slab is None:
        return None
    geom = _slab_geometry(slab, meta)
    if geom is None:
        return None
    spacing, z_top, c = geom
    z: float | None = None
    cart = op.get("cart")
    frac = op.get("frac")
    try:
        if isinstance(cart, (list, tuple)) and len(cart) >= 3:
            z = float(cart[2])
        elif isinstance(frac, (list, tuple)) and len(frac) >= 3:
            z = float(frac[2]) * c
    except (TypeError, ValueError):
        return None
    if z is None or spacing <= 0:
        return None
    depth = round((z_top - z) / spacing) + 1
    return depth if 1 <= depth <= slab["nz"] else None


def _render_mods(mods: list[dict[str, Any]]) -> list[str]:
    """Group identical modifications (first-occurrence order kept) and
    render each run with a count: ``'+ 2 Cu (surface substitution)'``."""
    runs: list[list[Any]] = []  # [mod, count] pairs
    for m in mods:
        for run in runs:
            r = run[0]
            if (
                r["kind"] == m["kind"]
                and r.get("what") == m.get("what")
                and r.get("words") == m.get("words")
                and r.get("depth") == m.get("depth")
                and r.get("colocated") == m.get("colocated")
            ):
                run[1] += 1
                break
        else:
            runs.append([m, 1])
    return [_render_run(m, n) for m, n in runs]


def _render_run(m: dict[str, Any], n: int) -> str:
    kind = m["kind"]
    count = f"{n} " if n > 1 else ""
    if kind in ("subst", "add"):
        return f"{count}{m['what']} ({m['words']})"
    if kind == "adatom":
        words = f" ({m['words']})" if m.get("words") else ""
        return f"{count}{m['what']} adatom{words}"
    if kind == "ads":
        words = f" ({m['words']})" if m.get("words") else ""
        return f"{count}{m['what']}{words}"
    # vacancy
    words = _depth_words(m.get("depth"))
    if m.get("colocated"):
        words = f"{words}, co-located" if words else "co-located"
    noun = "vacancies" if n > 1 else "vacancy"
    return f"{count}{noun}" + (f" ({words})" if words else "")


def _from_ops(ops: list[dict[str, Any]], meta: dict[str, Any]) -> str | None:
    """The label off the recorded recipe. ``mods`` collects one entry per
    modification op in recipe order (a ``None`` tombstone where a later
    op undid one); ``slab_depth`` maps each slab-atom label to its depth
    from the top; ``near_labels`` is every label a placement op named as
    its neighbour, for the vacancy's co-location pass at the end."""
    host = facet = None
    hi: dict[str, int] = {}
    slab_depth: dict[str, int] = {}
    slab_state: dict[str, Any] | None = None
    trusted = True
    mods: list[dict[str, Any] | None] = []
    sub_pos: dict[str, int] = {}
    near_labels: set[str] = set()
    vac_idx: list[int] = []
    for i, op in enumerate(ops):
        name = op.get("op")
        near = _near_label(op)
        if near:
            near_labels.add(near)
        if name == "slab":
            el, size = op.get("element"), op.get("size")
            if not (isinstance(el, str) and el.strip()):
                continue
            if not isinstance(size, (list, tuple)) or len(size) != 3:
                continue
            try:
                nx, ny, nz = (int(size[k]) for k in range(3))
            except (TypeError, ValueError):
                continue
            if nx * ny * nz <= 0 or nx * ny <= 0:
                continue
            host, facet = el.strip(), "111"
            try:
                vacuum = float(op["vacuum"]) if op.get("vacuum") is not None else 10.0
                a = float(op["a"]) if op.get("a") is not None else None
            except (TypeError, ValueError):
                vacuum, a = 10.0, None
            slab_state = {"nx": nx, "ny": ny, "nz": nz, "vacuum": vacuum, "a": a}
            # A slab is a fresh base — it clears the scene, so earlier
            # modifications and layer bookkeeping reset; the label
            # high-water does NOT (labels are never re-minted).
            mods.clear()
            sub_pos.clear()
            near_labels.clear()
            vac_idx.clear()
            slab_depth.clear()
            trusted = True
            start = hi.get(host, 0)
            for atom_i in range(1, nx * ny * nz + 1):
                layer = (atom_i - 1) // (nx * ny) + 1  # 1 = bottom, nz = top
                slab_depth[f"a{host}{start + atom_i}"] = nz - layer + 1
            hi[host] = start + nx * ny * nz
        elif name == "set_element":
            el = _element_of(op)
            if el is None:
                continue
            atom = op.get("atom")
            site = op.get("site")
            if atom is not None:
                key = f"atom:{atom}"
                depth = slab_depth.get(str(atom)) if trusted else None
            elif isinstance(site, dict):
                key = f"site:{i}"  # unique per op — precis picks the atom
                depth = _SUBST_DEPTH.get(str(site.get("type")))
            else:
                continue
            words = _subst_words(depth)
            if key in sub_pos:
                idx = sub_pos[key]
                if el == host:
                    # transmuted back to the host — the dopant is gone
                    mods[idx] = None
                    del sub_pos[key]
                else:
                    mods[idx] = {"kind": "subst", "what": el, "words": words}
            elif el != host:
                sub_pos[key] = len(mods)
                mods.append({"kind": "subst", "what": el, "words": words})
        elif name in ("add_atom", "add_atom_site"):
            el = _element_of(op)
            if el is None:
                continue
            if "label" not in op:
                hi[el] = hi.get(el, 0) + 1
            if el == host:
                continue  # more host atoms are never a modification
            if name == "add_atom":
                marker = op.get("site")
                if marker in ("subsurface", "interstitial"):
                    if marker == "interstitial":
                        words = "interstitial"
                    else:
                        # The marker records intent, not depth — the layer
                        # is real only when the record's geometry places
                        # the atom there, else the plain intent word.
                        depth = _marker_depth(op, slab_state, meta)
                        words = _depth_words(depth) or "subsurface"
                    mods.append({"kind": "add", "what": el, "words": words})
                elif el in _ADSORBATE_ELEMENTS:
                    mods.append({"kind": "ads", "what": el, "words": ""})
                else:
                    mods.append({"kind": "adatom", "what": el, "words": ""})
            else:
                site = op.get("site")
                t = (
                    str(site.get("type"))
                    if isinstance(site, dict) and site.get("type")
                    else ""
                )
                kind = "ads" if el in _ADSORBATE_ELEMENTS else "adatom"
                mods.append({"kind": kind, "what": el, "words": t})
        elif name == "add_adsorbate":
            sp = op.get("species")
            if not isinstance(sp, str) or not sp.strip():
                continue
            site = op.get("site")
            t = (
                str(site.get("type"))
                if isinstance(site, dict) and site.get("type")
                else ""
            )
            mods.append({"kind": "ads", "what": sp.strip(), "words": t})
            counts = _formula_counts(sp.strip())
            if counts is None:
                trusted = False
            else:
                for e, n in counts.items():
                    hi[e] = hi.get(e, 0) + n
        elif name == "vacancy":
            atom = op.get("atom")
            if atom is None:
                continue
            atom = str(atom)
            depth = slab_depth.get(atom) if trusted else None
            if depth is None:
                site = op.get("site")
                if isinstance(site, dict):
                    depth = _SUBST_DEPTH.get(str(site.get("type")))
            coloc = bool(op.get("near")) or any(op.get(k) for k in _COLOCATED_KEYS)
            # A vacancy of a substituted atom removes the dopant too.
            dead = f"atom:{atom}"
            if dead in sub_pos:
                mods[sub_pos.pop(dead)] = None
            mods.append(
                {"kind": "vacancy", "depth": depth, "colocated": coloc, "atom": atom}
            )
            vac_idx.append(len(mods) - 1)
        elif name == "ring":
            el = _element_of(op)
            ring_n = op.get("n")
            ring_n = int(ring_n) if isinstance(ring_n, (int, float, str)) else 0
            if el is not None and ring_n > 0:
                hi[el] = hi.get(el, 0) + ring_n
        elif name in _UNCOUNTABLE_MINTS:
            trusted = False
        # every other op mints no atoms and rewrites no labels — nothing
        # the bookkeeping needs
    for i in vac_idx:
        m = mods[i]
        if m is not None and m["atom"] in near_labels:
            m["colocated"] = True
    live = [m for m in mods if m]
    if host is None:
        host = _majority(meta.get("composition"))
    if host is None:
        return None
    head = f"{host}({facet})" if facet else host
    if not live:
        return f"{head}, clean"
    return f"{head} + " + " + ".join(_render_mods(live))


def _from_meta(meta: dict[str, Any]) -> str | None:
    """The label off ``meta`` alone — the ``params`` param-space stamp
    (dopant count/site, co-adsorbates), then the ``site_intent`` marker
    map, with ``composition`` naming the host. Coarser than the recipe
    path: no facet, no per-element dopant split, no vacancies."""
    if not meta:
        return None
    host = _majority(meta.get("composition"))
    params = meta.get("params")
    params = params if isinstance(params, dict) else {}
    mods: list[str] = []
    dop = params.get("dopant")
    if isinstance(dop, str) and dop:
        els = [e.strip() for e in dop.split(",") if e.strip()]
        words = {"subst": "substitution", "adatom": "adatom"}.get(
            str(params.get("site"))
        ) or ("mixed sites" if params.get("site") == "mixed" else "modification")
        if len(els) == 1:
            n = params.get("n_dopant")
            n = int(n) if isinstance(n, (int, float)) and int(n) > 1 else 0
            mods.append(f"{str(n) + ' ' if n else ''}{els[0]} ({words})")
        else:
            mods.extend(f"{el} ({words})" for el in els)
    coads = params.get("coads")
    coads_site = params.get("coads_site")
    if isinstance(coads, dict):
        for sp, n in sorted(coads.items()):
            site = coads_site.get(sp) if isinstance(coads_site, dict) else None
            words = (
                ""
                if site in (None, "frac")
                else ("mixed sites" if site == "mixed" else str(site))
            )
            count = f"{int(n)} " if isinstance(n, (int, float)) and int(n) > 1 else ""
            mods.append(f"{count}{sp}" + (f" ({words})" if words else ""))
    if not params:
        # ``meta.site_intent`` — the below-surface ``add_atom`` markers
        # (gr549760) — survives on refs whose params were never stamped.
        intent = meta.get("site_intent")
        if isinstance(intent, dict):
            by_kind: dict[tuple[str, str], int] = {}
            for label, marker in intent.items():
                m = _LABEL_RE.match(str(label))
                if not m or m.group(1) == host:
                    continue
                key = (m.group(1), str(marker))
                by_kind[key] = by_kind.get(key, 0) + 1
            for (el, marker), n in sorted(by_kind.items()):
                mods.append(f"{str(n) + ' ' if n > 1 else ''}{el} ({marker})")
    if host is None:
        return None
    if not mods:
        comp = meta.get("composition")
        if isinstance(comp, dict) and len(comp) == 1:
            return f"{host}, clean"
        return None
    return f"{host} + " + " + ".join(mods)


__all__ = ["catalyst_label"]
