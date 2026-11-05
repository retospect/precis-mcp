"""Solver-free declarations of thin-gap measure computers.

Each physics owns one group of slug → implementation names. Properties can
advertise every slug without importing numpy/scipy or the adapter. The flow
adapter loads these functions only when its computer table is first needed.
Empty groups let physics that modify existing outputs register no new measure.
"""

from __future__ import annotations

from collections.abc import Callable

COMPUTER_GROUPS: dict[str, tuple[str, dict[str, str]]] = {}
_SUBSCRIBERS: list[Callable[[str, dict[str, str]], None]] = []


def subscribe_computers(callback: Callable[[str, dict[str, str]], None]) -> None:
    """Populate a consumer and keep later registrations visible to it."""
    for module, computers in COMPUTER_GROUPS.values():
        callback(module, computers)
    _SUBSCRIBERS.append(callback)


def register_computers(name: str, module: str, computers: dict[str, str]) -> None:
    """Declare a group's import targets without importing the module."""
    if name in COMPUTER_GROUPS:
        raise ValueError(f"flow computer group {name!r} already registered")
    occupied = {slug for _, group in COMPUTER_GROUPS.values() for slug in group}
    overlap = occupied.intersection(computers)
    if overlap:
        raise ValueError(f"flow computers already registered: {sorted(overlap)}")
    COMPUTER_GROUPS[name] = module, dict(computers)
    for callback in _SUBSCRIBERS:
        callback(module, computers)


register_computers(
    "flow",
    "precis_se.flow",
    {
        "breakthrough-fraction": "_flow_breakthrough",
        "residence-time-t0": "_flow_residence",
        "resistance-ratio": "_flow_resistance_ratio",
        "evenness": "_flow_evenness",
    },
)
register_computers(
    "species",
    "precis_se.flow_species",
    {
        "outlet-conversion": "_flow_outlet_conversion",
        "outlet-concentration": "_flow_outlet_concentration",
        "wall-flux-fraction": "_flow_wall_flux_fraction",
        "damkohler": "_flow_damkohler",
    },
)
