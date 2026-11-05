"""Thin-gap physics declarations and tested combinations, independent of adapters.

These names are inputs to the flow spec, not the native solid-field dispatch
registry. A declaration registers token keys, evaluate keys and input measures;
presence activates its physics even when another physics' output is requested.
Empty registrations are valid. New combinations enter TESTED with their tests.
Computer implementations stay in the se adapter and card builders in regime.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Physics:
    """The input vocabulary and evaluate outputs owned by one thin-gap physics."""

    name: str
    token_keys: tuple[str, ...] = ()
    spec_keys: tuple[str, ...] = ()
    input_measures: tuple[str, ...] = ()
    measurands: tuple[str, ...] = ()


PHYSICS: dict[str, Physics] = {}
# Thin-gap extensions remain under the flow dispatch name (M2).
MEASURANDS: dict[str, tuple[str, ...]] = {"flow": ()}


def register(physics: Physics) -> None:
    """Declare one physics, refusing ambiguous key/output ownership."""
    if physics.name in PHYSICS:
        raise ValueError(f"flow physics {physics.name!r} already registered")
    for surface in ("token_keys", "spec_keys", "measurands"):
        occupied = {key for p in PHYSICS.values() for key in getattr(p, surface)}
        overlap = occupied.intersection(getattr(physics, surface))
        if overlap:
            raise ValueError(
                f"flow physics {surface} already registered: {sorted(overlap)}"
            )
    PHYSICS[physics.name] = physics
    MEASURANDS["flow"] = keys("measurands")


def keys(surface: str) -> tuple[str, ...]:
    """Registered vocabulary in declaration order (also the error options)."""
    return tuple(key for p in PHYSICS.values() for key in getattr(p, surface))


def active_physics(
    *,
    token_keys: Iterable[str] = (),
    spec_keys: Iterable[str] = (),
    input_measures: Iterable[str] = (),
) -> frozenset[str]:
    """Detect declared inputs, including zero-valued or unset input measures."""
    tokens, spec, measures = set(token_keys), set(spec_keys), set(input_measures)
    return frozenset({"flow"}) | frozenset(
        p.name
        for p in PHYSICS.values()
        if tokens.intersection(p.token_keys)
        or spec.intersection(p.spec_keys)
        or measures.intersection(p.input_measures)
    )


def combination_refusal(
    active: frozenset[str], *, lumen: bool = False
) -> tuple[str, str] | None:
    """Return the common cause and next step; adapters choose their error type."""
    if active in TESTED and (not lumen or active == frozenset({"flow"})):
        return None
    inputs = sorted(
        {
            key
            for name in active - {"flow"}
            if name in PHYSICS
            for key in (
                *PHYSICS[name].token_keys,
                *PHYSICS[name].spec_keys,
                *PHYSICS[name].input_measures,
            )
        }
    )
    names = ", ".join(sorted(active))
    cause = f"physics.untested_combination: {{{names}}}"
    if lumen:
        cause += " on flow:lumen"
    hint = "drop the declared inputs " + (", ".join(inputs) or names)
    return cause, hint + " to return to the tested {flow} case"


register(
    Physics(
        "flow",
        token_keys=(
            "in",
            "out",
            "dv",
            "profile",
            "zplane",
            "fluid",
            "cells",
            "seed",
            "n",
            "seed_z",
        ),
        spec_keys=(
            "inlet",
            "outlet",
            "dv",
            "profile",
            "zplane",
            "n",
            "frames",
            "seed_z",
            "evenness_region",
            "fluid",
            "measures",
        ),
        input_measures=("inlet-velocity", "pressure-drop"),
        measurands=(
            "breakthrough-fraction",
            "residence-time-t0",
            "resistance-ratio",
            "evenness",
        ),
    )
)
register(
    Physics(
        "species",
        token_keys=("species",),
        input_measures=(
            "inlet-concentration",
            "species-diffusivity",
            "wall-rate-constant",
        ),
    )
)

# Species outputs remain se computers; evaluate gains no new measurands here.
TESTED: set[frozenset[str]] = {frozenset({"flow"}), frozenset({"flow", "species"})}
