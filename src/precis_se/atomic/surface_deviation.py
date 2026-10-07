"""Expose the built S1 judge over stored atoms and an explicit authored target.

Explicit targets retain caller authoring and rigid-z semantics. Omitted
targets use only a generated-exact row-bound receipt from the same SQL
snapshot as the atoms; legacy scene/plan is not enough. The recorded affine
isometry includes reflection, never fitting/PCA/reconstruction on read.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.structure.cell import Cell
from precis_surface.deviation import Feature, summary, surface_distance
from precis_surface.revolution import authored_meridian


def _number(value: Any, address: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BadInput(f"{address} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise BadInput(f"{address} must be a finite number") from exc
    if not math.isfinite(number):
        raise BadInput(f"{address} must be a finite number")
    return number


def _features(target: Any) -> list[Feature]:
    if not isinstance(target, dict) or set(target) != {"features"}:
        raise BadInput("target requires only features=[{name, centre_A, r0_A, pieces}]")
    if not isinstance(target["features"], list):
        raise BadInput(
            "target.features must be a list (empty means an authored flat sheet)"
        )
    features: list[Feature] = []
    names = {"sheet"}
    for i, raw in enumerate(target["features"]):
        address = f"target.features[{i}]"
        if not isinstance(raw, dict) or set(raw) != {
            "name",
            "centre_A",
            "r0_A",
            "pieces",
        }:
            raise BadInput(f"{address} requires only name, centre_A, r0_A, pieces")
        name = raw["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise BadInput(f"{address}.name must be unique, nonblank, and not 'sheet'")
        names.add(name)
        centre = raw["centre_A"]
        if not isinstance(centre, list) or len(centre) != 2:
            raise BadInput(f"{address}.centre_A must be [x_A, y_A]")
        xy = (
            _number(centre[0], address + ".centre_A[0]"),
            _number(centre[1], address + ".centre_A[1]"),
        )
        r0 = _number(raw["r0_A"], address + ".r0_A")
        if r0 <= 0:
            raise BadInput(f"{address}.r0_A must be positive")
        pieces = raw["pieces"]
        if not isinstance(pieces, list) or not pieces:
            raise BadInput(f"{address}.pieces must be nonempty line/arc pieces")
        parsed: list[tuple[str, float] | tuple[str, float, float]] = []
        for j, piece in enumerate(pieces):
            where = f"{address}.pieces[{j}]"
            if not isinstance(piece, list) or not piece:
                raise BadInput(
                    f"{where} must be ['line', L_A] or ['arc', R_A, turn_deg]"
                )
            if piece[0] == "line" and len(piece) == 2:
                parsed.append(("line", _number(piece[1], where)))
            elif piece[0] == "arc" and len(piece) == 3:
                parsed.append(
                    ("arc", _number(piece[1], where), _number(piece[2], where))
                )
            else:
                raise BadInput(
                    f"{where} must be ['line', L_A] or ['arc', R_A, turn_deg]"
                )
        try:
            meridian = authored_meridian(r0, parsed)
        except ValueError as exc:
            raise BadInput(f"{address}: {exc}") from exc
        features.append(Feature(name, xy, meridian))
    return features


def render_surface_deviation(
    store: Any, node: Any, args: dict[str, Any], *, design_slug: str | None = None
) -> str:
    """Read-only S1 integration; unknown inputs never become zero/PASS."""
    header = f"# authored-surface deviation — block {node.name!r}"
    header += (
        f"\n\nsource: se:{design_slug or 'unknown'}; "
        f"block UID={'#' + str(node.uid) if node.uid is not None else 'unknown'}"
    )
    offset = _number(args.get("z_offset_A", 0), "z_offset_A")
    explicit = "target" in args
    features = _features(args["target"]) if explicit else []
    if not explicit and offset != 0:
        raise BadInput(
            "nonzero z_offset_A requires an explicit caller target; stored target uses only its recorded map",
            next="supply target={'features': [...]} with z_offset_A, or omit both for the stored target",
        )
    if node.template is not None or node.array is not None:
        return (
            header + "\n\nunknown: template/array instance frame is not authored here"
        )
    if node.bound_kind != "structure" or not node.bound:
        return header + "\n\nunknown: no bound structure coordinates"
    ref = store.get_ref(kind="structure", id=node.bound)
    if ref is None:
        return header + "\n\nunknown: bound structure is missing"
    # Same-version imports defeat version brackets. Label and coordinates
    # must come from the same statement snapshot, never structure_load.
    ref_id = ref.id
    snapshot = store.structure_positions_snapshot(ref_id)
    if snapshot is None or snapshot["ref_id"] != ref_id:
        return (
            header
            + "\n\nunknown: bound structure snapshot identity unavailable; retry this read"
        )
    version = snapshot["version"]
    if type(version) is not int or version < 1:
        return (
            header
            + "\n\nunknown: structure version identity unavailable; retry this read"
        )
    target_label = "target: caller-authored request; original generation target provenance unverified"
    alignment = f"alignment: subtract rigid z_offset_A={offset!r} Å only; no fitted rotation/scale or target substitution"
    mapping = None
    if not explicit:
        from precis_se.atomic.surface_target import stored_target

        try:
            features, q, b = stored_target(snapshot)
            mapping = (q, b)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return (
                header + f"\n\nunknown: stored authored target unavailable: {exc}. "
                "Supply target.features explicitly in structure-local Å, or use a future generation with an exact target receipt; "
                "legacy data is never fitted, rebuilt or relaxed automatically."
            )
        target_label = f"target: generated-exact receipt format=1, bound ref_id={ref_id}, version={version}; actual evaluated target, not requested top_R"
        alignment = f"alignment: recorded row-vector y=x@Q+b in Å; Q={q.tolist()!r}, b={b.tolist()!r}; determinant={float(np.linalg.det(q))!r}; no fitted map"
    try:
        cell = Cell(np.asarray(snapshot["lattice"], dtype=np.float64))
        points = np.asarray(
            [cell.frac_to_cart(frac) for frac in snapshot["fractional"]],
            dtype=np.float64,
        )
    except (TypeError, ValueError):
        return header + "\n\nunknown: stored coordinates are malformed"
    if (
        not len(points)
        or points.shape != (len(points), 3)
        or not np.isfinite(points).all()
    ):
        return (
            header + "\n\nunknown: stored coordinates are empty/nonfinite or malformed"
        )
    if mapping is not None:
        points = points @ mapping[0] + mapping[1]
        if not np.isfinite(points).all():
            return (
                header
                + "\n\nunknown: generated target map overflow; supply an explicit target"
            )
    try:
        # All authored pieces here are lines/arcs; S1's analytic path is exact,
        # and ds affects only catenoids (not accepted by authored_meridian).
        distances, owners = surface_distance(points, features, ds=1.0, z_offset=offset)
        if not explicit and not np.isfinite(distances).all():
            return (
                header
                + "\n\nunknown: stored target evaluation is nonfinite; supply an explicit target"
            )
        rows = summary(distances, owners, features)
        if not explicit and any(
            not np.isfinite([row[k] for k in ("mean", "p95", "max")]).all()
            for row in rows.values()
        ):
            return (
                header
                + "\n\nunknown: stored target evaluation is nonfinite; supply an explicit target"
            )
    except ValueError as exc:
        if not explicit:
            return (
                header
                + f"\n\nunknown: stored target evaluation unavailable: {exc}; supply an explicit target"
            )
        raise BadInput(f"authored target: {exc}") from exc
    table = [{"region": name, **values} for name, values in rows.items()]
    return (
        header
        + f"\n\ncoordinates: structure:{node.bound}, version={version}; structure-local Å; SE pose/rotation/scale not applied"
        + "\n"
        + target_label
        + "\n"
        + alignment
        + "\njudge: precis_surface.deviation.surface_distance + summary (existing S1); distances in Å; no bar/stability verdict\n\n"
        + render_agent_table(table, schema=["region", "atoms", "mean", "p95", "max"])
    )
