"""Board profiles: a bundle of overrides on existing pcb seams.

``pcb_boards.meta['profile']`` is ``'prod'`` (absent, today's behaviour
exactly) or ``'prototype'`` (tuned for hand assembly, probing and rework).
The numbers live in ``src/precis/data/pcb_profiles.json``, one row per
profile, each rule carrying a one-line reason. Consumers read a profile
through :func:`profile_rules` only; none branches on the profile name.

``prod`` is the all-``None`` / all-off :class:`ProfileRules`. A rule that is
``None`` (or an empty set) is off: the consumer keeps its own default. A set
value is a floor/ceiling applied on top of the fab capability row, never
below ``jlc_min``.

The capability overlay (:func:`precis.pcb.capabilities.apply_profile`) and
DRC strictness consume it; router cost, thermals and silk arrive in later
slices of docs/backlog/pcb-board-profile-prototype.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

from precis.pcb.capabilities import FIELDS as _CAPABILITY_FIELDS
from precis.pcb.capabilities import load_capabilities

_PACKAGED_DATA = "precis.data"
_FILE = "pcb_profiles.json"

DEFAULT_PROFILE = "prod"

#: Rule names a profile may enable; the DRC registry (slice 2/3) reads them.
KNOWN_CHECKS = frozenset(
    {"starved_thermal", "via_density", "via_under_part", "fine_pitch_pad"}
)

#: Scalar rule -> the ``ProfileRules`` attribute it fills. Integers are
#: listed apart so the loader can refuse a fractional spoke count.
_FLOAT_RULES = (
    "trace_width_mm",
    "trace_spacing_mm",
    "drill_mm",
    "annular_ring_mm",
    "via_body_cost_mm",
    "via_density_max_per_mm2",
    "via_density_window_mm",
    "via_density_step_mm",
    "thermal_spoke_width_mm",
    "thermal_gap_mm",
    "refdes_height_mm",
)
_INT_RULES = ("thermal_spokes",)


@dataclass(frozen=True)
class ProfileRules:
    """One profile's rules. ``None`` / empty means the rule is off."""

    name: str
    label: str = ""
    trace_width_mm: float | None = None
    trace_spacing_mm: float | None = None
    drill_mm: float | None = None
    annular_ring_mm: float | None = None
    via_body_cost_mm: float | None = None
    via_density_max_per_mm2: float | None = None
    via_density_window_mm: float | None = None
    via_density_step_mm: float | None = None
    thermal_spokes: int | None = None
    thermal_spoke_width_mm: float | None = None
    thermal_gap_mm: float | None = None
    refdes_height_mm: float | None = None
    #: Capability fields whose ``house_default`` shortfall is an error.
    strict_fields: frozenset[str] = frozenset()
    #: Extra DRC rule names this profile emits.
    checks: frozenset[str] = frozenset()
    #: rule name -> the one-line reason it is set.
    reasons: dict[str, str] = field(default_factory=dict, compare=False)


def _load_raw() -> dict[str, Any]:
    raw = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _check_floors(rules: ProfileRules) -> None:
    """A profile figure below even the loosest fab row's ``jlc_min`` is a
    data error and is refused at load. A figure below a *stricter* row's
    floor (aluminum needs a 0.65 mm drill) is legal: the overlay only
    raises (:func:`precis.pcb.capabilities.apply_profile`), so that row
    keeps its own floor."""
    rows = load_capabilities()
    for key in ("trace_width_mm", "trace_spacing_mm", "drill_mm", "annular_ring_mm"):
        value = getattr(rules, key)
        floors = [f for r in rows if (f := r.jlc_min.get(key)) is not None]
        if value is not None and floors and value < min(floors):
            raise ValueError(
                f"profile {rules.name!r}: {key} {value} is below every "
                f"fab row's jlc_min (loosest {min(floors)})"
            )


def _build(row: dict[str, Any]) -> ProfileRules:
    name = row["name"]
    kwargs: dict[str, Any] = {"name": name, "label": row.get("label", "")}
    reasons: dict[str, str] = {}
    rules = row.get("rules", {})
    unknown = set(rules) - set(_FLOAT_RULES) - set(_INT_RULES)
    if unknown:
        raise ValueError(f"profile {name!r}: unknown rule(s) {sorted(unknown)}")
    for key, spec in rules.items():
        value, why = spec.get("value"), str(spec.get("why", "")).strip()
        if not why:
            raise ValueError(f"profile {name!r}: rule {key!r} has no reason")
        if key in _INT_RULES:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"profile {name!r}: {key!r} must be an int >= 1")
        elif (
            not isinstance(value, int | float) or isinstance(value, bool) or value <= 0
        ):
            raise ValueError(f"profile {name!r}: {key!r} must be a number > 0")
        else:
            value = float(value)
        kwargs[key] = value
        reasons[key] = why
    for key, allowed in (
        ("strict_fields", set(_CAPABILITY_FIELDS)),
        ("checks", set(KNOWN_CHECKS)),
    ):
        block = row.get(key)
        if block is None:
            continue
        names = block.get("fields" if key == "strict_fields" else "rules", [])
        bad = set(names) - allowed
        if bad:
            raise ValueError(f"profile {name!r}: {key} names unknown {sorted(bad)}")
        if names and not str(block.get("why", "")).strip():
            raise ValueError(f"profile {name!r}: {key} has no reason")
        kwargs[key] = frozenset(names)
        reasons[key] = str(block.get("why", "")).strip()
    return ProfileRules(reasons=reasons, **kwargs)


@lru_cache(maxsize=1)
def _profiles() -> dict[str, ProfileRules]:
    out = {}
    for row in _load_raw()["rows"]:
        rules = _build(row)
        _check_floors(rules)
        out[rules.name] = rules
    if DEFAULT_PROFILE not in out:
        raise ValueError(f"{_FILE}: no {DEFAULT_PROFILE!r} row")
    return out


def profile_names() -> tuple[str, ...]:
    """Every profile name, file order (``prod`` first)."""
    return tuple(_profiles())


def profile_rules(name: str | None) -> ProfileRules:
    """The rules for ``name``; ``None`` / blank is ``prod``. Raises
    ``ValueError`` for an unknown name, listing the valid ones."""
    key = (name or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
    try:
        return _profiles()[key]
    except KeyError:
        raise ValueError(
            f"unknown pcb profile {name!r}; options: {list(_profiles())}"
        ) from None


def board_profile(board_meta: dict[str, Any] | None) -> str:
    """The profile name authored on a board's ``meta`` (absent ⇒ ``prod``)."""
    return str((board_meta or {}).get("profile") or DEFAULT_PROFILE)
