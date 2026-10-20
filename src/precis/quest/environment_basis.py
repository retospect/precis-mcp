"""The kinetics v2 environment basis of a stored pathway config (D1).

``environment_basis(config)`` is sha256 over the canonical D1 tuple
``(environment_schema=2, T, p_total, inputs, outputs, inerts, sticking,
electrochemical)`` after ``p_total`` scaling — byte-for-byte the engine's
``autocatpath.environment.environment_basis``. It is re-implemented here
because :mod:`precis.quest.compute` is the engine-free half of the bridge:
the harvest that stamps network bases may run on a host without the
``catalyst`` extra, and a host-dependent basis (null where the engine is
absent) would make every margin harvested there incomparable.
``tests/test_pathway_environment.py`` pins parity with the engine over
valid, legacy, preset and invalid configs wherever the engine is installed.

A config with an ``environment:`` block projects that block; one without
projects its legacy keys (``conditions.pressures`` as ``inputs``,
``kinetics.sticking`` as uncited entries, ``unlisted: reference``), so
every existing pathway gets a basis with no backfill. ``None`` when the
config cannot be projected (the engine would refuse it); a ``None`` basis
never compares (:func:`precis.quest.frontier.same_network_basis`).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

ENVIRONMENT_SCHEMA = 2

_BLOCK_KEYS = {"T", "p_total", "gases", "sticking", "electrochemical"}
_GAS_KEYS = {"inputs", "outputs", "inerts", "unlisted"}


class _Refused(ValueError):
    """The engine would refuse this block; the basis is ``None``."""


def _num(x: Any) -> float:
    if isinstance(x, bool):
        return float(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise _Refused(repr(x)) from None
    return v


def _table(name: str, raw: Any) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _Refused(name)
    out: dict[str, float] = {}
    for gas, p in raw.items():
        v = _num(p)
        if not math.isfinite(v) or v < 0 or (v == 0 and name in ("inputs", "outputs")):
            raise _Refused(f"{name}[{gas!r}]")
        out[str(gas)] = v
    return out


def _sticking(raw: Any) -> dict[str, list[Any]]:
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        raise _Refused("sticking")
    out: dict[str, list[Any]] = {}
    for gas, entry in raw.items():
        if isinstance(entry, Mapping):
            if set(entry) - {"s0", "source"} or "s0" not in entry:
                raise _Refused(f"sticking[{gas!r}]")
            s0, source = entry["s0"], entry.get("source")
        else:
            s0, source = entry, None
        v = _num(s0)
        if not 0.0 < v <= 1.0:
            raise _Refused(f"sticking[{gas!r}]")
        out[str(gas)] = [v, None if source is None else str(source)]
    return out


def _block(data: Any) -> list[Any]:
    if not isinstance(data, Mapping) or set(data) - _BLOCK_KEYS or "T" not in data:
        raise _Refused("environment")
    gases = data.get("gases") or {}
    if not isinstance(gases, Mapping) or set(gases) - _GAS_KEYS:
        raise _Refused("environment.gases")
    raw_out = gases.get("outputs")
    far = False
    if isinstance(raw_out, str):
        if raw_out != "far":
            raise _Refused("environment.gases.outputs")
        far, outputs = True, {}
    else:
        outputs = _table("outputs", raw_out)
    if str(gases.get("unlisted", "refuse")) not in ("refuse", "reference"):
        raise _Refused("environment.gases.unlisted")
    inputs = _table("inputs", gases.get("inputs"))
    inerts = _table("inerts", gases.get("inerts"))
    if set(inputs) & set(outputs):
        raise _Refused("environment.gases")
    ec = data.get("electrochemical")
    if ec is not None and not isinstance(ec, Mapping):
        raise _Refused("environment.electrochemical")
    return _tuple(
        data["T"], data.get("p_total"), inputs, outputs, far, inerts,
        _sticking(data.get("sticking")), ec,
    )  # fmt: skip


def _tuple(
    t_raw: Any,
    p_total_raw: Any,
    inputs: dict[str, float],
    outputs: dict[str, float],
    far: bool,
    inerts: dict[str, float],
    sticking: dict[str, list[Any]],
    electrochemical: Any,
) -> list[Any]:
    t = _num(t_raw)
    if not (math.isfinite(t) and t > 0):
        raise _Refused("environment.T")
    table_sum = math.fsum([*inputs.values(), *outputs.values(), *inerts.values()])
    if p_total_raw is None:
        p_total, f = None, 1.0
    else:
        p_total = _num(p_total_raw)
        if not (math.isfinite(p_total) and p_total > 0) or table_sum <= 0:
            raise _Refused("environment.p_total")
        f = p_total / table_sum

    def scaled(table: dict[str, float]) -> dict[str, float]:
        return dict(sorted((g, v * f) for g, v in table.items()))

    return [
        ENVIRONMENT_SCHEMA,
        t,
        p_total if p_total is not None else table_sum,
        scaled(inputs),
        "far" if far else scaled(outputs),
        scaled(inerts),
        dict(sorted(sticking.items())),
        electrochemical,
    ]


def basis_tuple(config: Mapping[str, Any]) -> list[Any]:
    """The D1 tuple of ``config`` (block or legacy keys); raises
    ``ValueError`` when the engine would refuse it."""
    if config.get("environment") is not None:
        return _block(config["environment"])
    cond = config.get("conditions") or {}
    ec = config.get("electrochemistry") or {}
    if not isinstance(cond, Mapping) or not isinstance(ec, Mapping):
        raise _Refused("conditions")
    # the engine reads these as `(x or {}).get/.items()`: a falsy value of
    # any type is empty, a truthy non-mapping is refused (AttributeError)
    kin = config.get("kinetics") or {}
    if not isinstance(kin, Mapping):
        raise _Refused("kinetics")
    sticking = kin.get("sticking")
    if sticking and not isinstance(sticking, Mapping):
        raise _Refused("kinetics.sticking")
    pressures = cond.get("pressures") or {}
    if not isinstance(pressures, Mapping):
        raise _Refused("conditions.pressures")
    return _tuple(
        cond.get("temperature", ec.get("T", 298.15)),
        None,
        {str(k): _num(v) for k, v in pressures.items()},
        {},
        False,
        {},
        {str(g): [_num(v), None] for g, v in (sticking or {}).items()},
        None,
    )


def environment_basis(config: Mapping[str, Any]) -> str | None:
    """sha256 hex of :func:`basis_tuple`, or ``None`` when ``config`` cannot
    be projected."""
    try:
        blob = json.dumps(
            basis_tuple(config), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError, AttributeError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["ENVIRONMENT_SCHEMA", "basis_tuple", "environment_basis"]
