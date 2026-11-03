"""Profile-vs-kinetics conditions labelling for ``pathway`` reads (gr553328).

A pathway carries two condition sets at once. The energy profile and the
energetic span are priced at the profile's own gas table — the run's
``config.conditions`` (recorded back on ``results.thermo.pressures``),
unlisted gases at the 1 bar reference — while the steady-state kinetics
solve applies the D1 ``environment`` block recorded on
``results.kinetics_v2`` (or the v1 block's ``conditions``), with its own
temperature and partial pressures. Nothing on the page or in the MCP
views named the difference, so a profile ΔG compared against the TOF
read an unpriced ``kT·ln(p/p°)`` shift (~0.3 eV at p = 1e-5 bar) as
chemistry.

:func:`conditions_note` is the one-line caveat the profile and kinetics
sections both carry; :func:`methods_conditions` is the ``## Conditions``
paragraph appended to the Methods body. Both read only stored record
values — no recomputation — and both return ``None`` on a pathway
without a recorded kinetics environment, so kinetics-less pathways keep
their previous text. The closing sentence follows the recorded tables
(``_kt_sentence``): an empty profile table means the 1 bar standard
state, so the profile truly excludes the kinetics' gas terms; a priced
profile table of its own means only the *difference* between the two
tables is excluded; equal table and T means the conditions match.

Shared neutral ground: ``precis_web`` never imports ``precis_pathway``
(an external plugin kind), so the web page and the MCP views render off
this module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

#: The v2 environment's gas tables, in display order (mirrors
#: ``precis_pathway.toon_views._environment_line`` / the web overlay's
#: ``_v2_overlay`` conditions fold).
_GAS_TABLE_KEYS = ("inputs", "outputs", "inerts")


def _f(x: Any) -> float | None:
    """A finite number, or ``None`` — a stored non-finite/absent value is
    no condition to state."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _num(x: Any) -> str:
    v = _f(x)
    return "?" if v is None else f"{v:g}"


def _gas_table(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(g): p for g, v in raw.items() if (p := _f(v)) is not None and p > 0}


def kinetics_environment(results: Mapping[str, Any]) -> dict[str, Any] | None:
    """The environment the kinetics solve ran at, off ``meta.results`` —
    ``{T, pressures, p_total, preset, legacy, basis}`` or ``None``.

    The v2 record answers first; the v1 ``kinetics`` block fills what it
    lacks (the same shallow merge ``kinetics_text`` applies), and a
    v1-only solve still yields its ``conditions`` (``temperature`` +
    ``pressures``). ``pressures`` flattens the stated partial pressures
    (bar) across inputs/outputs/inerts; ``legacy`` flags a pre-D1 record
    whose unstated gases sat at the 1 bar reference; ``basis`` is the
    environment-basis hash truncated to 12 chars (the
    ``_environment_line`` convention).
    """
    kin = results.get("kinetics")
    merged: dict[str, Any] = dict(kin) if isinstance(kin, Mapping) else {}
    v2 = results.get("kinetics_v2")
    if isinstance(v2, Mapping):
        merged.update({k: v for k, v in v2.items() if v is not None})
    basis = merged.get("environment_basis")
    basis = str(basis)[:12] if isinstance(basis, str) and basis else None
    env = merged.get("environment")
    if isinstance(env, Mapping) and env:
        gases = env.get("gases")
        gases = gases if isinstance(gases, Mapping) else {}
        pressures: dict[str, float] = {}
        for key in _GAS_TABLE_KEYS:
            pressures.update(_gas_table(gases.get(key)))
        return {
            "T": _f(env.get("T")),
            "pressures": pressures,
            "p_total": _f(env.get("p_total")),
            "preset": gases.get("preset"),
            "legacy": bool(env.get("legacy")),
            "basis": basis,
        }
    cond = merged.get("conditions")
    if isinstance(cond, Mapping) and cond:
        return {
            "T": _f(cond.get("temperature")),
            "pressures": _gas_table(cond.get("pressures")),
            "p_total": None,
            "preset": None,
            "legacy": False,
            "basis": basis,
        }
    return None


def profile_conditions(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The conditions the energy profile/span were priced at: ``{T,
    pressures}``. ``T`` is the recorded profile temperature — the same
    chain the engine's own energy-tier methods sentence follows
    (``results.thermo.temperature``, else
    ``config.conditions.temperature``; ``results.T`` is the
    *electrochemistry* lever's T, not the profile's) — ``None`` when the
    record carries neither, never a fabricated default. ``pressures`` is
    the gas table the stored nodes were priced at
    (``results.thermo.pressures``, the same table
    ``precis_pathway.runner._stored_node_pressures`` reads) — empty or
    unrecorded means the 1 bar reference."""
    results = meta.get("results")
    results = results if isinstance(results, Mapping) else {}
    config = meta.get("config")
    config = config if isinstance(config, Mapping) else {}
    thermo = results.get("thermo")
    thermo = thermo if isinstance(thermo, Mapping) else {}
    t = _f(thermo.get("temperature"))
    if t is None:
        cond = config.get("conditions")
        if isinstance(cond, Mapping):
            t = _f(cond.get("temperature"))
    pressures: dict[str, float] = {}
    if thermo.get("mode") not in (None, "none"):
        pressures = _gas_table(thermo.get("pressures"))
    return {"T": t, "pressures": pressures}


def _profile_phrase(meta: Mapping[str, Any]) -> str:
    pc = profile_conditions(meta)
    t = f"T = {_num(pc['T'])} K" if pc["T"] is not None else "T not recorded"
    gases = (
        ", ".join(f"p({g}) = {p:g} bar" for g, p in sorted(pc["pressures"].items()))
        or "all gases at the 1 bar reference"
    )
    return f"{t}, {gases}"


def _kinetics_phrase(env: Mapping[str, Any]) -> str:
    t = (
        f"T = {_num(env.get('T'))} K"
        if _f(env.get("T")) is not None
        else "T not recorded"
    )
    parts = [t]
    if env.get("p_total") is not None:
        parts.append(f"p_total = {env['p_total']:g} bar")
    pressures = env.get("pressures") or {}
    if pressures:
        parts.append(
            ", ".join(f"p({g}) = {p:g} bar" for g, p in sorted(pressures.items()))
        )
    if env.get("preset"):
        parts.append(f"preset {env['preset']}")
    if env.get("legacy"):
        parts.append("unlisted gases at the 1 bar reference (legacy)")
    if env.get("basis"):
        parts.append(f"basis {env['basis']}")
    return "; ".join(parts)


def _kt_sentence(profile: Mapping[str, Any], env: Mapping[str, Any]) -> str:
    """The closing sentence, chosen by what the RECORDED tables say
    (gr553328 review): the profile excludes the kinetics' gas terms only
    when it was priced at the 1 bar standard state — a profile priced at
    its own table already includes kT·ln(p/p°) terms, so there only the
    difference to the kinetics' table is excluded."""
    same_gases = profile["pressures"] == env["pressures"]
    same_t = (
        profile["T"] is not None and env["T"] is not None and profile["T"] == env["T"]
    )
    if same_gases and same_t:
        return (
            "The kinetics solved at the same temperature and gas table the "
            "energy profile is priced at — the two are directly comparable."
        )
    if not profile["pressures"] and not env["pressures"]:
        # 1-bar gases on both sides; only the temperature can differ.
        return (
            "Both the energy profile and the kinetics price gases at the 1 "
            "bar reference — a ΔG difference between them is thermal (the "
            "temperature), not a kT·ln(p/p°) gas term."
        )
    if not profile["pressures"]:
        return (
            "The energy profile's ΔG excludes the kT·ln(p/p°) gas terms the "
            "kinetics solve includes — the two are not directly comparable."
        )
    return (
        "The energy profile's ΔG carries its own gas table's kT·ln(p/p°) "
        "terms; the kinetics reprices each gas at its stated pressures, so "
        "only the difference between the two tables is excluded — the two "
        "are not directly comparable."
    )


def conditions_note(meta: Mapping[str, Any]) -> str | None:
    """The one-line profile-vs-kinetics caveat for the energy-profile and
    kinetics sections (``gr553328`` fix 2), or ``None`` when the pathway
    recorded no kinetics environment."""
    results = meta.get("results")
    if not isinstance(results, Mapping):
        return None
    env = kinetics_environment(results)
    if env is None:
        return None
    return (
        f"conditions: energy profile and span at {_profile_phrase(meta)}; "
        f"kinetics at {_kinetics_phrase(env)}. "
        f"{_kt_sentence(profile_conditions(meta), env)}"
    )


def methods_conditions(
    meta: Mapping[str, Any], *, body: str | None = None
) -> str | None:
    """The ``## Conditions`` block appended to the Methods text
    (``gr553328`` fix 1): the profile's stated T and gas table beside the
    kinetics environment's T and partial pressures, plus the case-chosen
    ``kT·ln(p/p°)`` sentence — or ``None`` on a pathway without a
    recorded kinetics environment (the Methods text then stays as
    written).

    ``body`` is the stored Methods chunk text when the caller has it: a
    boilerplate sentence there hard-codes 298.15 K / the 1 bar reference,
    so when the record disagrees the block adds a line saying the listed
    values are the recorded ones."""
    results = meta.get("results")
    if not isinstance(results, Mapping):
        return None
    env = kinetics_environment(results)
    if env is None:
        return None
    pc = profile_conditions(meta)
    out = (
        "## Conditions\n\n"
        f"- Energy profile and span: {_profile_phrase(meta)}.\n"
        f"- Kinetics: {_kinetics_phrase(env)}.\n\n"
        f"{_kt_sentence(pc, env)}"
    )
    if body:
        t_hardcoded = "298.15" in body and pc["T"] is not None and pc["T"] != 298.15
        p_hardcoded = "1 bar" in body and bool(pc["pressures"])
        if t_hardcoded or p_hardcoded:
            out += (
                "\n\nThe Methods text above hard-codes 298.15 K / the 1 bar "
                "reference; the values listed here are the recorded ones."
            )
    return out
