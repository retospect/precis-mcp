"""Exact generated targets and row-bound receipts (gr470905).

No target is fitted or reconstructed on read. The version-1 codec stores
analytic evaluated line/arc segments and a row-vector affine isometry,
including reflection. SHA256 binds that record to a particular live atom
row set, not just a version. Canonical UTF-8 JSON has sorted keys, compact
separators, finite float.hex strings and positive-zero normalization;
integer identities remain integers. No per-atom metadata copy is retained.
The 1e-12 map tolerance is serialization roundoff, not a physical bar.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from precis_surface.deviation import Feature
from precis_surface.revolution import Meridian, Segment

MAP_ROUNDOFF = 1e-12


def _array(raw: Any, shape: tuple[int, ...]) -> np.ndarray:
    if not isinstance(raw, list):
        raise ValueError("target/frame array missing")

    # Booleans are not numeric geometry, even though numpy accepts them.
    def numbers(value: Any) -> bool:
        if isinstance(value, list):
            return all(numbers(v) for v in value)
        return type(value) in (int, float)

    if not numbers(raw):
        raise ValueError("target/frame array is not numeric")
    try:
        arr = np.asarray(raw, dtype=float)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("malformed target/frame array") from exc
    if arr.shape != shape or not np.isfinite(arr).all():
        raise ValueError("malformed/nonfinite target/frame array")
    return arr


def capture_target(
    features: tuple[Feature, ...], tops: dict[str, Any]
) -> dict[str, Any]:
    """Capture the evaluated primitives, including actual R/dome-start facts."""
    rows = []
    for f in features:
        segments = []
        for seg in f.meridian.segments:
            if seg.arc is None and seg.kind not in ("flat", "cylinder", "cone"):
                raise ValueError("unsupported generated target primitive")
            segments.append(
                {
                    "kind": seg.kind,
                    "name": seg.name,
                    "sign": seg.sign,
                    "line": [list(seg.start), list(seg.end)]
                    if seg.arc is None
                    else None,
                    "arc": list(seg.arc) if seg.arc is not None else None,
                }
            )
        rows.append({"name": f.name, "centre_A": list(f.centre), "segments": segments})
    return {"format": 1, "units": "angstrom", "features": rows, "actual_tops": tops}


def decode_target(
    record: Any, *, permit_sheet_name: bool = False
) -> tuple[list[Feature], np.ndarray, np.ndarray]:
    if (
        not isinstance(record, dict)
        or type(record.get("format")) is not int
        or record["format"] != 1
    ):
        raise ValueError("unsupported/missing generated target format")
    if record.get("units") != "angstrom":
        raise ValueError("unsupported generated target units")
    tops = record.get("actual_tops")
    if not isinstance(tops, dict):
        raise ValueError("evaluated top provenance missing")
    for name, top in tops.items():
        if not isinstance(name, str) or not isinstance(top, dict):
            raise ValueError("malformed evaluated top provenance")
        values = _array([top.get("R_A"), top.get("dome_start_A")], (2,))
        if values[0] <= 0:
            raise ValueError("invalid evaluated top radius")
    mapping = record.get("map")
    if (
        not isinstance(mapping, dict)
        or mapping.get("convention") != "row-vector y=x@Q+b"
    ):
        raise ValueError("generated target frame missing")
    q = _array(mapping.get("Q"), (3, 3))
    b = _array(mapping.get("b_A"), (3,))
    if (
        not np.allclose(q.T @ q, np.eye(3), atol=MAP_ROUNDOFF, rtol=0)
        or abs(abs(float(np.linalg.det(q))) - 1) > MAP_ROUNDOFF
    ):
        raise ValueError("generated target map is not an isometry")
    raw_features = record.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise ValueError("generated target features missing")
    features = []
    names: set[str] = set()
    for raw in raw_features:
        if not isinstance(raw, dict):
            raise ValueError("malformed generated feature")
        name = raw.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in names
            or (name == "sheet" and not permit_sheet_name)
        ):
            raise ValueError("ambiguous generated feature name")
        names.add(name)
        centre = _array(raw.get("centre_A"), (2,))
        pieces = raw.get("segments")
        if not isinstance(pieces, list) or not pieces:
            raise ValueError("generated meridian missing")
        segments = []
        previous = None
        for piece in pieces:
            if (
                not isinstance(piece, dict)
                or not isinstance(piece.get("name"), str)
                or type(piece.get("sign")) is not int
                or piece["sign"] not in (-1, 0, 1)
            ):
                raise ValueError("malformed generated segment")
            if piece.get("arc") is not None:
                arc = _array(piece["arc"], (5,))
                if (
                    piece.get("kind") != "fillet"
                    or piece.get("line") is not None
                    or arc[2] <= 0
                    or arc[3] == arc[4]
                ):
                    raise ValueError("unsupported generated arc")

                def arc_param(
                    t: np.ndarray, a: np.ndarray = arc
                ) -> tuple[np.ndarray, np.ndarray]:
                    phi = a[3] + (a[4] - a[3]) * t
                    return a[0] + a[2] * np.cos(phi), a[1] + a[2] * np.sin(phi)

                seg = Segment(
                    "fillet",
                    piece["name"],
                    arc_param,
                    sign=piece["sign"],
                    arc=(
                        float(arc[0]),
                        float(arc[1]),
                        float(arc[2]),
                        float(arc[3]),
                        float(arc[4]),
                    ),
                )
                lo, hi = sorted((float(arc[3]), float(arc[4])))
                minimum = min(seg.start[0], seg.end[0])
                if math.ceil((lo - math.pi) / math.tau) * math.tau + math.pi <= hi:
                    minimum = min(minimum, float(arc[0] - arc[2]))
            else:
                if piece.get("kind") not in ("flat", "cylinder", "cone"):
                    raise ValueError("unsupported generated line")
                line = _array(piece.get("line"), (2, 2))
                if np.array_equal(line[0], line[1]):
                    raise ValueError("empty generated line")

                def line_param(
                    t: np.ndarray, a: np.ndarray = line
                ) -> tuple[np.ndarray, np.ndarray]:
                    p = a[0] + t[..., None] * (a[1] - a[0])
                    return p[..., 0], p[..., 1]

                seg = Segment(
                    piece["kind"], piece["name"], line_param, sign=piece["sign"]
                )
                minimum = min(seg.start[0], seg.end[0])
            if minimum < -MAP_ROUNDOFF or (
                previous is not None
                and not np.allclose(previous, seg.start, atol=MAP_ROUNDOFF, rtol=0)
            ):
                raise ValueError("discontinuous/axis-crossing generated meridian")
            previous = seg.end
            if not np.isfinite([*seg.start, *seg.end]).all():
                raise ValueError("nonfinite evaluated generated segment")
            segments.append(seg)
        features.append(
            Feature(
                name,
                (float(centre[0]), float(centre[1])),
                Meridian(tuple(segments), ()),
            )
        )
    if not tops.keys() <= names:
        raise ValueError("evaluated top does not match a generated feature")
    return features, q, b


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("non-string receipt key")
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite receipt/binding")
        return "f64:" + (0.0 if value == 0 else value).hex()
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("unsupported receipt value")


def geometry_binding(
    record: dict[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    ref_id, version = snapshot.get("ref_id"), snapshot.get("version")
    if (
        type(ref_id) is not int
        or ref_id <= 0
        or type(version) is not int
        or version < 1
    ):
        raise ValueError("structure identity/version missing")
    ids, fractions = snapshot.get("atom_ids"), snapshot.get("fractional")
    if (
        not isinstance(ids, list)
        or not ids
        or any(type(i) is not int or i <= 0 for i in ids)
        or ids != sorted(set(ids))
    ):
        raise ValueError("persisted atom row identity missing")
    coords = _array(fractions, (len(ids), 3))
    if snapshot.get("has_lattice") is not True:
        raise ValueError("stored cell missing")
    cell = _array(snapshot.get("lattice"), (3, 3))
    pbc = snapshot.get("pbc")
    if (
        not isinstance(pbc, list)
        or len(pbc) != 3
        or any(type(v) is not bool for v in pbc)
    ):
        raise ValueError("stored periodicity missing")
    payload = {
        "format": 1,
        "ref_id": ref_id,
        "version": version,
        "units": "angstrom",
        "lattice": cell.tolist(),
        "pbc": pbc,
        "atoms": [[i, *p] for i, p in zip(ids, coords.tolist(), strict=True)],
        "target": {k: v for k, v in record.items() if k != "binding"},
    }
    encoded = json.dumps(
        _canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "format": 1,
        "ref_id": ref_id,
        "version": version,
        "atoms": len(ids),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def bind_target(record: dict[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    # Scene authoring permits a feature called "sheet". Preserve its
    # successful generation; S1's reserved region name makes that receipt
    # ambiguous on read, where it remains explicitly unavailable.
    decode_target(record, permit_sheet_name=True)
    return {**record, "binding": geometry_binding(record, snapshot)}


def stored_target(
    snapshot: Mapping[str, Any],
) -> tuple[list[Feature], np.ndarray, np.ndarray]:
    generated = snapshot.get("generated")
    if not isinstance(generated, dict) or generated.get("generator") != "hexfold_scene":
        raise ValueError("matching generated scene receipt absent")
    record = generated.get("surface_target")
    if not isinstance(record, dict):
        raise ValueError("matching generated scene receipt absent")
    decoded = decode_target(record)
    binding = record.get("binding")
    if (
        not isinstance(binding, dict)
        or any(
            type(binding.get(k)) is not int
            for k in ("format", "ref_id", "version", "atoms")
        )
        or not isinstance(binding.get("sha256"), str)
        or binding != geometry_binding(record, snapshot)
    ):
        raise ValueError("generated target identity/row/geometry binding is stale")
    return decoded
