"""Spec-derived surface target for a legacy ``hexfold_scene`` block.

Used by :func:`precis_se.atomic.surface_deviation.render_surface_deviation`
only when the exact evaluated receipt (:mod:`.surface_target`) is absent or
unusable — blocks minted before the receipt existed carry no recorded frame,
so the target is *derived*, not recovered (gr470905):

- the stored ``spec`` (the block's regeneration input) is rebuilt once,
  deterministically — never relaxed — for its instance layout and the seed
  positions an authored top's dome start is measured on;
- the meridians are the surfaces the scene planner authored (flat run,
  foot fillet, tube wall; an authored top's recorded *actual* R/fillet/drop
  from ``plan.top_plans``), never a fitted or requested surface;
- placement in the stored frame is measured on the named instances' stored
  atoms: the ``s`` sheet atoms must sit in a plane within
  ``_SHEET_PLANAR_A`` of their mean (the only proof that the stored frame's
  z is the scene axis — a scene whose canonical frame picked an in-plane
  axis fails here), every feature must sit on the same side of that plane,
  and each feature's centre is the mean ``(x, y)`` of its tube's atoms;
- the stored row order is bound loosely — element identity per row, total
  count and the termination record — weaker than the receipt's row digest,
  which is why the result is labelled derived, not recorded provenance.

Anything the derivation cannot establish raises ``ValueError`` and renders
as an honest ``unknown``, never a guessed placement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from hexfold.build import build
from hexfold.lattice import tube_radius
from hexfold.report import HexfoldError
from precis.structure.cell import Cell
from precis_se.atomic.generators._types import GeneratorError
from precis_se.atomic.generators.authored_foot import (
    _SCENE_FLAT_A,
    _WALL_A,
    SceneFeature,
    _authored_top,
    _top_meridian,
    _top_names,
)
from precis_se.atomic.generators.hexfold_scene import _feature
from precis_surface import revolution as rv
from precis_surface.deviation import Feature

#: A tethered sheet stays within a bond length or two of its mean plane; a
#: stored frame whose z is *not* the scene axis shows a z spread of order
#: the sheet's own extent (tens of Å) instead.
_SHEET_PLANAR_A = 5.0
#: The mean tube position must clear the sheet plane by at least this to
#: call which side the feature grows on.
_FEATURE_SIDE_A = 1.0


def _scene_feature(raw: Any, idx: int) -> SceneFeature:
    """The generator's own parser: a stored feature dict that would not
    parse as an authored scene feature (including a ``type``-routed one)
    cannot describe a target."""
    try:
        return _feature(raw, idx)
    except GeneratorError as exc:
        raise ValueError(str(exc)) from exc


def _top_record(
    generated: Mapping[str, Any], f: SceneFeature
) -> tuple[float, float, float, str]:
    """The recorded actual top numbers ``_top_meridian`` needs: the chosen
    candidate's kind, realised R, fillet and dome drop."""
    plan = generated.get("plan")
    top_plans = plan.get("top_plans") if isinstance(plan, dict) else None
    tp = top_plans.get(f.name) if isinstance(top_plans, dict) else None
    if not isinstance(tp, dict) or tp.get("kind") not in ("sphere", "lid"):
        raise ValueError(f"{f.name}: stored plan carries no authored-top record")
    try:
        big_r, fillet, drop = (
            float(tp["R"]),
            float(tp["fillet"]),
            float(tp["drop"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{f.name}: stored authored-top record is malformed") from exc
    if not all(map(math.isfinite, (big_r, fillet, drop))) or fillet <= 0 or big_r <= 0:
        raise ValueError(f"{f.name}: stored authored-top record is nonfinite")
    return big_r, fillet, drop, str(tp["kind"])


def _frame(
    inst: np.ndarray, points: np.ndarray, features: tuple[SceneFeature, ...]
) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float]]]:
    """Derived stored-to-judge map: measured sheet plane, measured grow
    side, measured per-feature centres. ``y = x @ Q + b`` like the receipt,
    ``Q = diag(1, 1, t)`` — the stored frame must already be the scene
    frame, which the sheet's planarity check proves or the read refuses."""
    sheet = np.flatnonzero(inst == "s")
    if not len(sheet):
        raise ValueError("the rebuilt spec has no sheet instance 's'")
    sheet_z = points[sheet, 2]
    z_sheet = float(sheet_z.mean())
    spread = float(np.abs(sheet_z - z_sheet).max())
    if spread > _SHEET_PLANAR_A:
        raise ValueError(
            f"the stored frame is not the scene frame: sheet atoms span "
            f"{spread:.1f} A about their mean z (allowed {_SHEET_PLANAR_A:g} A)"
        )
    centres: list[tuple[float, float]] = []
    sides: set[int] = set()
    for f in features:
        ords = np.flatnonzero(inst == f.name)
        if not len(ords):
            raise ValueError(
                f"{f.name}: the feature instance is absent from the rebuilt spec"
            )
        tube = points[ords]
        centres.append((float(tube[:, 0].mean()), float(tube[:, 1].mean())))
        side = float(tube[:, 2].mean()) - z_sheet
        if abs(side) < _FEATURE_SIDE_A:
            raise ValueError(
                f"{f.name}: the feature's atoms sit on the sheet plane; "
                "the grow side is ambiguous"
            )
        sides.add(1 if side > 0 else -1)
    if len(sides) != 1:
        raise ValueError("the features sit on both sides of the sheet")
    (t,) = sides
    return (
        np.diag([1.0, 1.0, float(t)]),
        np.array([0.0, 0.0, -t * z_sheet]),
        centres,
    )


def _rows_match_layout(
    generated: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    n_c: int,
) -> tuple[list[Any], list[Any]]:
    """Loose row-order binding: the live rows must be the spec's atoms in
    order (element identity per row, capped count, H tail), weaker than the
    receipt's SHA256 row digest by design."""
    if type(generated.get("n_atoms")) is not int or generated["n_atoms"] != n_c:
        raise ValueError("stored atom count does not match the rebuilt spec")
    terminated = generated.get("terminated")
    capped = terminated.get("count", 0) if isinstance(terminated, dict) else 0
    if type(capped) is not int or capped < 0:
        raise ValueError("stored termination record is malformed")
    fractional = snapshot.get("fractional")
    elements = snapshot.get("elements")
    if (
        not isinstance(fractional, list)
        or not isinstance(elements, list)
        or len(fractional) != n_c + capped
        or len(elements) != len(fractional)
    ):
        raise ValueError("stored rows do not match the rebuilt spec's atom layout")
    return fractional, elements


def derived_target(
    snapshot: Mapping[str, Any],
) -> tuple[list[Feature], np.ndarray, np.ndarray]:
    """Derive the authored target of a ``hexfold_scene`` block that has no
    usable receipt: rebuilt spec for the instance layout, stored scene/plan
    for the surfaces, measured placement on the stored atoms."""
    generated = snapshot.get("generated")
    if not isinstance(generated, dict) or generated.get("generator") != "hexfold_scene":
        raise ValueError("stored record is not a hexfold_scene generation")
    spec = generated.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("stored hexfold spec missing")
    scene = generated.get("scene")
    raw_features = scene.get("features") if isinstance(scene, dict) else None
    if not isinstance(raw_features, list) or not raw_features:
        raise ValueError("stored scene features missing")
    features_in = tuple(_scene_feature(r, i) for i, r in enumerate(raw_features))

    # The deterministic regeneration input only: instance layout and seed
    # positions (the authored top's dome start). No relaxation runs on read.
    try:
        net = build(spec, strict=False)
    except HexfoldError as exc:
        raise ValueError(f"stored spec does not rebuild: {exc}") from exc
    except (ValueError, KeyError, IndexError) as exc:
        raise ValueError(f"stored spec does not rebuild: {exc}") from exc
    if net.seed3 is None:
        raise ValueError("rebuilt spec carries no seed positions")
    seed = np.asarray(net.seed3, dtype=float)
    n_c = len(net.atoms)
    fractional, elements = _rows_match_layout(generated, snapshot, n_c)
    if any(elements[i] != a.element for i, a in enumerate(net.atoms)) or any(
        e != "H" for e in elements[n_c:]
    ):
        raise ValueError("stored rows are not the spec's atoms in order")
    try:
        cell = Cell(np.asarray(snapshot.get("lattice"), dtype=np.float64))
        points = np.asarray(
            [cell.frac_to_cart(frac) for frac in fractional], dtype=np.float64
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("stored coordinates are malformed") from exc
    if points.shape != (len(fractional), 3) or not np.isfinite(points).all():
        raise ValueError("stored coordinates are empty/nonfinite")

    inst = np.array([a.instance for a in net.atoms])
    q, b, centres = _frame(inst, points, features_in)
    features: list[Feature] = []
    for f, centre in zip(features_in, centres, strict=True):
        if f.radius <= 0:
            raise ValueError(f"{f.name}: stored fillet radius must be positive")
        rt = tube_radius(f.n, 0)
        if _authored_top(f):
            big_r, fillet, drop, kind = _top_record(generated, f)
            first = _top_names(f.name, f.top)[0]
            junc = sorted(
                {
                    a
                    for i, j, *_ in net.bonds
                    for a, o in ((i, j), (j, i))
                    if inst[a] == f.name and inst[o] == first
                }
            )
            if not junc:
                raise ValueError(f"{f.name}: the dome-start junction atoms are absent")
            z1 = float(-seed[junc, 2].mean()) - drop
            meridian = _top_meridian(kind, rt, f.radius, z1, big_r, fillet)
        else:
            meridian = rv.authored_meridian(
                rt + f.radius + _SCENE_FLAT_A,
                [
                    ("line", _SCENE_FLAT_A),
                    ("arc", f.radius, -90.0),
                    ("line", _WALL_A),
                ],
            )
        features.append(Feature(f.name, centre, meridian))
    return features, q, b


__all__ = ["derived_target"]
