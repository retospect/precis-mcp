"""One environment point for pathway reads, links and explicit re-solves.

The record uses the engine's D1 vocabulary, with electrochemical coordinates
kept separate from the gas reservoir. Hashing and pressure scaling reuse
``precis.quest.environment_basis``; no engine import or calculation is needed.
Legacy configs retain the reference-gas policy and project their stated
electrochemistry onto the point (the old engine hash omitted those fields).
An absent electrochemical coordinate stays unknown, never silently pH 7.

Canonical links carry ``env=<JSON D1 block>``. The existing ``u/ph`` and
``U/pH`` aliases can override that point; legacy pages retain their own
defaults and clamps before constructing a record. Windows describe a search
domain, not a point, and therefore do not enter the environment basis.

Lookup returns an exact stored record, adjacent complete sweep corners, or
none. It does not interpolate rates or dispatch jobs. A sweep only brackets
T and total pressure at its recorded composition and conditions. Rows without
a resolved environment establish only T and pressures, so additional requested
conditions cannot borrow the baseline TOF.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any
from urllib.parse import urlencode

from precis.errors import BadInput
from precis.quest.environment_basis import basis_tuple, environment_basis

_NEXT = "get(kind='skill', id='precis-pathway-help')"
_EC_COORDINATES = {"U_vs_RHE", "pH", "ion_conc_M"}
_EC_KEYS = _EC_COORDINATES | {"provider", "sigma_uC_cm2", "U_pzc", "cation"}


def _refuse(message: str) -> BadInput:
    return BadInput(f"environment: {message}", next=_NEXT)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _refuse(f"{name} must be a finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise _refuse(f"{name} must be a finite number") from None
    if not math.isfinite(value):
        raise _refuse(f"{name} must be a finite number")
    return value


@dataclass(frozen=True)
class Environment:
    """Validated D1 record; use the constructors to project stored sources."""

    _block: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self._block, Mapping):
            raise _refuse("expected a D1 object with T and gases")
        block = copy.deepcopy(dict(self._block))
        if _number(block.get("T"), "T") <= 0:
            raise _refuse("T must be positive (K)")
        block["T"] = float(block["T"])
        if block.get("p_total") is not None:
            block["p_total"] = _number(block["p_total"], "p_total")
            if block["p_total"] <= 0:
                raise _refuse("p_total must be positive (bar)")
        ec = block.get("electrochemical")
        if ec is not None:
            if not isinstance(ec, Mapping) or set(ec) - _EC_KEYS:
                raise _refuse(f"electrochemical accepts {', '.join(sorted(_EC_KEYS))}")
            ec = dict(ec)
            for key in _EC_COORDINATES & ec.keys():
                value = _number(ec[key], key)
                # The persisted basis hashes electrochemical values as given:
                # changing an integer to a float would change the lookup key.
                if isinstance(ec[key], str):
                    ec[key] = value
            if ec.get("provider") not in (None, "che"):
                raise _refuse("electrochemical.provider must be che or null")
            if ec.get("provider") == "che" and "U_vs_RHE" not in ec:
                raise _refuse("electrochemical.provider che needs finite U_vs_RHE")
            if "pH" in ec and not 0 <= ec["pH"] <= 14:
                raise _refuse("pH must be between 0 and 14")
            if "ion_conc_M" in ec and ec["ion_conc_M"] <= 0:
                raise _refuse("ion_conc_M must be positive (mol/L)")
            block["electrochemical"] = ec or None
        gases = block.get("gases") or {}
        if block.get("gases") is not None and not isinstance(block["gases"], Mapping):
            raise _refuse("gases must be an object")
        if isinstance(gases, Mapping):
            for side in ("inputs", "outputs", "inerts"):
                table = gases.get(side)
                if isinstance(table, Mapping):
                    for gas, pressure in table.items():
                        _number(pressure, f"gases.{side}.{gas}")
        sticking = block.get("sticking") or {}
        if block.get("sticking") is not None and not isinstance(
            block["sticking"], Mapping
        ):
            raise _refuse("sticking must be an object")
        if isinstance(sticking, Mapping):
            for gas, entry in sticking.items():
                _number(
                    entry.get("s0") if isinstance(entry, Mapping) else entry,
                    f"sticking.{gas}.s0",
                )
        try:
            basis_tuple({"environment": block})
            if environment_basis({"environment": block}) is None:
                raise _refuse("scaled pressures must be finite")
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise _refuse(
                "invalid D1 block; use positive gas pressures, nonnegative inerts, "
                "0 < sticking.s0 <= 1, and a gas composition for p_total"
            ) from None
        object.__setattr__(self, "_block", block)

    @classmethod
    def from_dict(cls, block: Mapping[str, Any]) -> Environment:
        if not isinstance(block, Mapping):
            raise _refuse("expected a D1 object with T and gases")
        return cls(dict(block))

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> Environment:
        """Project config.environment or the legacy conditions/electrochemistry."""
        if not isinstance(config, Mapping):
            raise _refuse("config must be an object")
        if config.get("environment") is not None:
            return cls.from_dict(config["environment"])
        try:
            _, t, _, inputs, outputs, inerts, sticking, _ = basis_tuple(config)
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise _refuse("legacy config cannot be projected") from None
        conditions = config.get("conditions") or {}
        electrochemistry = config.get("electrochemistry") or {}
        ec: dict[str, Any] = {}
        for name, old in (("U_vs_RHE", "potential_U"), ("pH", "pH")):
            value = conditions.get(old, electrochemistry.get(name))
            if value is not None:
                ec[name] = value
        if electrochemistry.get("ion_conc_M") is not None:
            ec["ion_conc_M"] = electrochemistry["ion_conc_M"]
        block: dict[str, Any] = {
            "T": t,
            "gases": {
                "inputs": inputs,
                "outputs": outputs,
                "inerts": inerts,
                "unlisted": "reference",
            },
            "sticking": {
                gas: {"s0": entry[0], "source": entry[1]}
                for gas, entry in sticking.items()
            },
        }
        if ec:
            block["electrochemical"] = ec
        return cls(block)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Environment:
        """Project a resolved kinetics_v2.environment (pressures already scaled)."""
        gases = record.get("gases") or {}
        if not isinstance(gases, Mapping):
            raise _refuse("record gases must be an object")
        block: dict[str, Any] = {"T": record.get("T")}
        block["gases"] = {
            k: copy.deepcopy(v)
            for k, v in gases.items()
            if k in {"inputs", "outputs", "inerts", "unlisted"}
        }
        if gases.get("preset") == "far":
            block["gases"]["outputs"] = "far"
        if record.get("legacy"):
            block["gases"]["unlisted"] = "reference"
        for key in ("sticking", "electrochemical"):
            if record.get(key) is not None:
                block[key] = copy.deepcopy(record[key])
        return cls(block)

    @classmethod
    def from_operating_conditions(cls, conditions: Mapping[str, Any]) -> Environment:
        """Quest operating point; U_RHE maps onto D1's U_vs_RHE."""
        block: dict[str, Any] = {
            "T": conditions.get("T", conditions.get("temperature", 298.15)),
            "gases": conditions.get(
                "gases",
                {
                    "inputs": conditions.get("pressures", {}),
                    "unlisted": "reference",
                },
            ),
        }
        for key in ("p_total", "sticking"):
            if key in conditions:
                block[key] = conditions[key]
        ec = {key: conditions[key] for key in _EC_KEYS if key in conditions}
        if "U_RHE" in conditions:
            ec["U_vs_RHE"] = conditions["U_RHE"]
        if ec:
            block["electrochemical"] = ec
        return cls(block)

    @classmethod
    def from_url(
        cls, params: Mapping[str, Any], *, default: Environment | None = None
    ) -> Environment:
        """Decode canonical env JSON plus legacy point aliases, strictly."""
        env = default or cls.from_config({})
        if params.get("env") is not None:
            try:
                block = json.loads(params["env"])
            except (ValueError, TypeError):
                raise _refuse("env must be a JSON D1 object") from None
            env = cls.from_dict(block)
        block = env.to_dict()
        ec = env.electrochemical
        for key, aliases in (("U_vs_RHE", ("u", "U")), ("pH", ("ph", "pH"))):
            for alias in aliases:
                if params.get(alias) is not None:
                    try:
                        ec[key] = float(params[alias])
                    except (TypeError, ValueError):
                        raise _refuse(f"{alias} must be a finite number") from None
                    break
        if ec:
            block["electrochemical"] = ec
        return cls(block)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._block)

    def to_url(self) -> str:
        return urlencode(
            {"env": json.dumps(self._block, sort_keys=True, separators=(",", ":"))}
        )

    @property
    def T(self) -> float:
        return float(self._block["T"])

    @property
    def electrochemical(self) -> dict[str, Any]:
        return dict(self._block.get("electrochemical") or {})

    @property
    def p_total(self) -> float:
        return float(basis_tuple({"environment": self._block})[2])

    @property
    def basis(self) -> str:
        basis = environment_basis({"environment": self._block})
        if basis is None:
            raise _refuse("record has no valid environment_basis")
        return basis

    def matches(self, other: Environment) -> bool:
        """Compare pressures at 1e-12 relative tolerance; other fields exactly.

        Resolved gas tables can lose the last bit of the stated total. Keep
        the engine-compatible hash unchanged, but allow that roundoff on reads.
        No absolute tolerance: dilute reservoirs must remain distinguishable.
        """
        left = basis_tuple({"environment": self._block})
        right = basis_tuple({"environment": other._block})
        if left[:2] != right[:2] or left[6:] != right[6:]:
            return False
        if not math.isclose(left[2], right[2], rel_tol=1e-12, abs_tol=0.0):
            return False
        for a, b in zip(left[3:6], right[3:6], strict=True):
            if isinstance(a, dict) and isinstance(b, dict):
                if a.keys() != b.keys() or any(
                    not math.isclose(a[k], b[k], rel_tol=1e-12, abs_tol=0.0) for k in a
                ):
                    return False
            elif a != b:
                return False
        return True


def _stored_point(meta: Mapping[str, Any], record: Mapping[str, Any]) -> Environment:
    config = meta.get("config") or {}
    conditions = record.get("conditions")
    if not isinstance(record.get("environment"), Mapping) and isinstance(
        conditions, Mapping
    ):
        # A pre-D1 re-solve may have run at a different T/p from the graph.
        config = {k: v for k, v in config.items() if k != "environment"}
        config["conditions"] = {**(config.get("conditions") or {}), **conditions}
    env = (
        Environment.from_record(record["environment"])
        if isinstance(record.get("environment"), Mapping)
        else Environment.from_config(config)
    )
    # Older kinetic exports omit the run's electrochemistry. Project only
    # coordinates actually stated in the config, never the requested point.
    raw_env = record.get("environment")
    legacy = not isinstance(raw_env, Mapping) or raw_env.get("legacy") is True
    if legacy and not env.electrochemical:
        ec = Environment.from_config(config).electrochemical
        if ec:
            env = Environment.from_dict({**env.to_dict(), "electrochemical": ec})
    return env


def _sweep_point(base: Environment | None, row: Mapping[str, Any]) -> Environment:
    if isinstance(row.get("environment"), Mapping):
        return Environment.from_record(row["environment"])
    if base is None:
        pressures = row.get("pressures")
        if not isinstance(pressures, Mapping) or not pressures:
            raise _refuse("sweep row has no recorded pressures")
        point = Environment.from_dict({"T": row["T"], "gases": {"inputs": pressures}})
        if not math.isclose(
            point.p_total,
            _number(row["p_total"], "sweep.p_total"),
            rel_tol=1e-12,
            abs_tol=0.0,
        ):
            raise _refuse("sweep row pressures do not sum to p_total")
        return point
    point = Environment.from_dict(
        {**base.to_dict(), "T": row["T"], "p_total": row["p_total"]}
    )
    pressures = row.get("pressures")
    if pressures is not None:
        if not isinstance(pressures, Mapping):
            raise _refuse("sweep pressures must be an object")
        values = basis_tuple({"environment": point.to_dict()})
        expected = {**values[3], **values[5]}
        if isinstance(values[4], dict):
            expected.update(values[4])
        if set(values[3]) - set(pressures):
            raise _refuse("sweep row is missing a feed pressure")
        for gas, pressure in pressures.items():
            if gas not in expected or not math.isclose(
                _number(pressure, f"pressures.{gas}"),
                expected[gas],
                rel_tol=1e-12,
                abs_tol=0.0,
            ):
                raise _refuse("sweep row composition differs from its base environment")
    return point


def lookup_environment(meta: Mapping[str, Any], env: Environment) -> dict[str, Any]:
    """Lookup only: exact stored solve, complete adjacent sweep cell, or none."""
    answer: dict[str, Any] = {"status": "none", "environment_basis": env.basis}
    results = meta.get("results") or {}
    for source in ("kinetics_v2", "kinetics", "kinetics_v2_previous"):
        record = results.get(source)
        if not isinstance(record, Mapping) or not record:
            continue
        try:
            point = _stored_point(meta, record)
        except BadInput:
            continue
        if point.matches(env):
            return {
                **answer,
                "status": "exact",
                "source": source,
                "record": dict(record),
            }
    bracket: dict[str, Any] | None = None
    for source in ("kinetics_sweep", "kinetics_sweep_previous"):
        sweep = results.get(source)
        if not isinstance(sweep, Mapping):
            continue
        found = _lookup_sweep(sweep, env, source)
        if found["status"] == "exact":
            return found
        # An exact previous point takes precedence over a current bracket.
        if found["status"] == "bracketed" and bracket is None:
            bracket = found
        if "reason" in found:
            answer["reason"] = found["reason"]
    return bracket if bracket is not None else answer


def _lookup_sweep(
    sweep: Mapping[str, Any], env: Environment, source: str
) -> dict[str, Any]:
    """Check one sweep without mixing its provenance or grid with another."""
    answer: dict[str, Any] = {"status": "none", "environment_basis": env.basis}
    # A degraded sweep can survive beside a newer solve. Its rows establish
    # T and pressures; only its own environment establishes other conditions.
    base: Environment | None = None
    try:
        if isinstance(sweep.get("environment"), Mapping):
            base = Environment.from_record(sweep["environment"])
    except BadInput:
        return answer
    rows: dict[tuple[float, float], dict[str, Any]] = {}
    grid: set[tuple[float, float]] = set()
    for row in sweep.get("points") or []:
        if not isinstance(row, dict):
            continue
        try:
            # Failed or differently composed rows still mark the grid: never
            # bridge across an interior slice whose results cannot be used.
            t = _number(row.get("T"), "sweep.T")
            p = _number(row.get("p_total"), "sweep.p_total")
            if t > 0 and p > 0:
                grid.add((t, p))
            if base is None and not isinstance(row.get("environment"), Mapping):
                requested = env.to_dict()
                unsupported = sorted(set(requested) - {"T", "p_total", "gases"})
                unsupported.extend(
                    f"gases.{key}"
                    for key in sorted(set(requested.get("gases") or {}) - {"inputs"})
                )
                if unsupported:
                    answer["reason"] = (
                        "Sweep has no stored environment establishing provenance for "
                        + ", ".join(unsupported)
                        + "; its rows establish only T and gas pressures."
                    )
                    continue
            point = _sweep_point(base, row)
            # Reprice only the swept coordinates to compare every other field.
            at_request = _sweep_point(point, {"T": env.T, "p_total": env.p_total})
            if at_request.matches(env):
                rate = row.get("tof_mf")
                if (
                    isinstance(rate, bool)
                    or not isinstance(rate, int | float)
                    or not math.isfinite(rate)
                    or row.get("error")
                ):
                    continue
                if point.matches(env):
                    answer.pop("reason", None)
                    return {
                        **answer,
                        "status": "exact",
                        "source": source,
                        "record": row,
                    }
                rows[(t, p)] = row
        except (BadInput, KeyError):
            continue
    bounds: dict[str, list[float]] = {}
    for index, (axis, value) in enumerate((("T", env.T), ("p_total", env.p_total))):
        values = sorted({point[index] for point in grid})
        if axis == "p_total":
            close = next(
                (
                    x
                    for x in values
                    if math.isclose(x, value, rel_tol=1e-12, abs_tol=0.0)
                ),
                None,
            )
            if close is not None:
                bounds[axis] = [close, close]
                continue
        lower = [x for x in values if x <= value]
        upper = [x for x in values if x >= value]
        if not lower or not upper:
            return answer
        bounds[axis] = [max(lower), min(upper)]
    corners = list(product(sorted(set(bounds["T"])), sorted(set(bounds["p_total"]))))
    if not corners or any(corner not in rows for corner in corners):
        return answer
    answer.pop("reason", None)
    return {
        **answer,
        "status": "bracketed",
        "source": source,
        "bounds": bounds,
        "corners": [rows[corner] for corner in corners],
    }
