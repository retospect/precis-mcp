"""Expose the built S1 judge over stored atoms and an explicit authored target.

Target provenance is caller authoring, not an inferred generation surface.
Structure-local Å coordinates stay unchanged except the supplied rigid z shift;
no fitted rotation/scale, geometry construction, persistence or relaxation.
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
    if args.get("target") is None:
        return (
            header
            + "\n\nunknown: authored target absent; supply target.features explicitly. No target inferred from stored geometry/planner."
        )
    features = _features(args["target"])
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
    try:
        # All authored pieces here are lines/arcs; S1's analytic path is exact,
        # and ds affects only catenoids (not accepted by authored_meridian).
        distances, owners = surface_distance(points, features, ds=1.0, z_offset=offset)
        rows = summary(distances, owners, features)
    except ValueError as exc:
        raise BadInput(f"authored target: {exc}") from exc
    table = [{"region": name, **values} for name, values in rows.items()]
    return (
        header
        + f"\n\ncoordinates: structure:{node.bound}, version={version}; structure-local Å; SE pose/rotation/scale not applied"
        + "\ntarget: caller-authored request; original generation target provenance unverified"
        + f"\nalignment: subtract rigid z_offset_A={offset!r} Å only; no fitted rotation/scale or target substitution"
        + "\njudge: precis_surface.deviation.surface_distance + summary (existing S1); distances in Å; no bar/stability verdict\n\n"
        + render_agent_table(table, schema=["region", "atoms", "mean", "p95", "max"])
    )
