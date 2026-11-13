"""The correction policy of a stored pathway, as a comparable tuple.

``correction_policy(pw_meta)`` projects which energy corrections an engine
at 0.24 or later applied to a pathway's energetics — the gas and H* tables,
the table version, the adsorbed-molecule delta, how the H* host was chosen
and the model keys the tables cover — into a JSON-native dict, so
:func:`precis.quest.frontier.same_network_basis` never ranks a margin
measured under one policy against another's. Catpath owns the canonical
policy function; this is the engine-free mirror, the pattern of
:mod:`precis.quest.environment_basis`. Stdlib only; it imports nothing from
:mod:`precis.quest.compute` or :mod:`precis.quest.frontier`.

The tuple is ``{"identity_schema": 1, "table_version", "gas_enabled",
"adsorbed_molecules", "h_star_enabled", "host_selection", "model_keys"}``,
compared by exact equality. The result record (``results.corrections``) is
authoritative: a part or switch absent inside a present record is OFF; a
record missing altogether on an engine at 0.24 or later is an error (the
pipeline always writes it), so the policy is unknown (``None``) and the fact
is returned as an issue; a config that contradicts the result does not make
the policy unknown — the result wins and an issue says so. Malformed
structure makes the policy unknown too: a non-bool switch, a model key that
is not a list of two or more strings, or a ``config.corrections`` that is
present and not an object (with H* on). A null or
unparseable ``autocatpath_version`` is unknown, never legacy. Unknown never
compares, not even with another unknown (:func:`same_correction_policy`);
a stamp without the ``correction_policy`` key predates the field and is
compatible with any other.
"""

from __future__ import annotations

import re
from typing import Any

IDENTITY_SCHEMA = 1

#: First engine that writes ``results.corrections``.
FIRST_POLICY_VERSION: tuple[int, ...] = (0, 24)

_ABSENT = object()


def _version_key(v: Any) -> tuple[int, ...]:
    if not isinstance(v, str):
        return ()
    m = re.match(r"\d+(?:\.\d+)*", v.strip())
    return tuple(int(p) for p in m.group().split(".")) if m else ()


def policy_era(version: Any) -> str:
    """``"legacy"`` below 0.24 (no policy is stamped), ``"current"`` from
    0.24 on, ``"unknown"`` for a null or unparseable version (R4)."""
    key = _version_key(version if version is None else str(version))
    if not key:
        return "unknown"
    return "current" if key >= FIRST_POLICY_VERSION else "legacy"


class _Unknown(Exception):
    """The policy cannot be determined; the message says why."""


def _part_enabled(record: dict[str, Any], part: str) -> tuple[bool, Any]:
    """``(enabled, part_dict)``; an absent part or ``enabled`` key is OFF."""
    body = record.get(part, _ABSENT)
    if body is _ABSENT or body is None:
        return False, {}
    if not isinstance(body, dict):
        raise _Unknown(f"results.corrections.{part} is not an object")
    enabled = body.get("enabled", _ABSENT)
    if enabled is _ABSENT:
        return False, body
    if not isinstance(enabled, bool):
        raise _Unknown(f"results.corrections.{part}.enabled is not a bool")
    return enabled, body


def _model_key(entry: Any, where: str) -> list[str]:
    if (
        not isinstance(entry, list)
        or len(entry) < 2
        or not all(isinstance(x, str) for x in entry)
    ):
        raise _Unknown(f"{where} holds a model key that is not a list of strings")
    return [entry[0], entry[1]]


def _model_keys(body: dict[str, Any], part: str, extra: str) -> list[list[str]]:
    out: list[list[str]] = []
    sets = body.get("sets", {})
    if sets is None:
        sets = {}
    if not isinstance(sets, dict):
        raise _Unknown(f"results.corrections.{part}.sets is not an object")
    for label, entry in sets.items():
        where = f"results.corrections.{part}.sets[{label!r}].key"
        if not isinstance(entry, dict) or "key" not in entry:
            raise _Unknown(f"{where} is missing")
        out.append(_model_key(entry["key"], where))
    listed = body.get(extra, [])
    if listed is None:
        listed = []
    if not isinstance(listed, list):
        raise _Unknown(f"results.corrections.{part}.{extra} is not a list")
    for entry in listed:
        out.append(_model_key(entry, f"results.corrections.{part}.{extra}"))
    return out


def _host_selection(config: dict[str, Any]) -> str:
    corr = config.get("corrections", _ABSENT)
    if corr is _ABSENT:
        return "auto"
    if not isinstance(corr, dict):
        raise _Unknown("config.corrections is not an object")
    host = corr.get("host_metal")
    if host is None:
        return "auto"
    if not isinstance(host, str):
        raise _Unknown("config.corrections.host_metal is not a string")
    return f"explicit:{host}"


def _contradictions(
    config: dict[str, Any], gas: bool, h_star: bool, adsorbed: Any
) -> list[str]:
    corr = config.get("corrections")
    if not isinstance(corr, dict):
        return []
    issues: list[str] = []
    checks: list[tuple[str, Any, str]] = [
        ("gas", gas, "gas.enabled"),
        ("h_star", h_star, "h_star.enabled"),
    ]
    if gas and isinstance(adsorbed, bool):
        checks.append(("adsorbed_molecules", adsorbed, "gas.adsorbed_molecules"))
    for key, effective, path in checks:
        cfg = corr.get(key)
        if isinstance(cfg, bool) and cfg != effective:
            issues.append(
                f"config.corrections.{key}={cfg} contradicts "
                f"results.corrections.{path}={effective}; the result is used"
            )
    return issues


def correction_policy(
    pw_meta: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """``(policy, issues)`` for a pathway's ``meta``.

    ``policy`` is the tuple described in the module docstring, ``None`` when
    unknown. ``issues`` are plain-language reasons: why the policy is
    unknown, or a config that contradicts the result (the policy stays
    known). On an engine below 0.24 a missing record is not an error, so
    callers gate on :func:`policy_era` before stamping."""
    meta = pw_meta if isinstance(pw_meta, dict) else {}
    version = meta.get("autocatpath_version")
    era = policy_era(version)
    if era == "unknown":
        return None, [f"autocatpath_version {version!r} is unknown or unparseable"]
    results = meta.get("results")
    results = results if isinstance(results, dict) else {}
    record = results.get("corrections")
    if not isinstance(record, dict):
        if era == "current":
            return None, [f"results.corrections missing on engine {version}"]
        return None, []
    config = meta.get("config")
    config = config if isinstance(config, dict) else {}
    try:
        gas, gas_body = _part_enabled(record, "gas")
        h_star, h_body = _part_enabled(record, "h_star")
        adsorbed: Any = None
        if gas:
            adsorbed = gas_body.get("adsorbed_molecules", False)
            if not isinstance(adsorbed, bool):
                raise _Unknown(
                    "results.corrections.gas.adsorbed_molecules is not a bool"
                )
        table: Any = None
        if gas or h_star:
            table = record.get("version")
            if not isinstance(table, str) or not table:
                raise _Unknown("results.corrections.version is not a non-empty string")
        host = _host_selection(config) if h_star else None
        keys: list[list[str]] = []
        if gas:
            keys += _model_keys(gas_body, "gas", "unknown")
        if h_star:
            keys += _model_keys(h_body, "h_star", "unanchored")
    except _Unknown as exc:
        return None, [str(exc)]
    model_keys = [list(k) for k in sorted({tuple(k) for k in keys})]
    policy = {
        "identity_schema": IDENTITY_SCHEMA,
        "table_version": table,
        "gas_enabled": gas,
        "adsorbed_molecules": adsorbed if gas else None,
        "h_star_enabled": h_star,
        "host_selection": host,
        "model_keys": model_keys,
    }
    return policy, _contradictions(config, gas, h_star, adsorbed)


def same_correction_policy(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two ``network_basis`` stamps agree on the correction policy.

    A present ``None`` is unknown and never compares, not even with another
    ``None`` or a keyless stamp; otherwise a stamp without the
    ``correction_policy`` key predates the field (or is the pinned basis)
    and is compatible with any other."""
    pa = a.get("correction_policy", _ABSENT)
    pb = b.get("correction_policy", _ABSENT)
    if pa is None or pb is None:
        return False
    if pa is _ABSENT or pb is _ABSENT:
        return True
    return bool(pa == pb)


__all__ = [
    "IDENTITY_SCHEMA",
    "correction_policy",
    "policy_era",
    "same_correction_policy",
]
