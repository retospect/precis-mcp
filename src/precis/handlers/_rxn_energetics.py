"""Render :mod:`precis.thermo` results for ``get(kind='rxn', view='energetics')``.

Stateless: parses ``q=`` (one equation, or several separated by ``;`` or
newlines = a pathway), calls the pure thermo core, returns agent text.
"""

from __future__ import annotations

import re
from typing import Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.response import Response
from precis.thermo import (
    F_CONST,
    PathwayLedger,
    ReactionResult,
    pathway_ledger,
    reaction_energetics,
)

_NEXT = (
    "get(kind='rxn', view='energetics', q='NO + 5/2 H2 -> NH3 + H2O', "
    "args={'T': 298.15, 'n_electrons': 5})"
)

_SPECIES_SCHEMA = [
    "species",
    "side",
    "coef",
    "phase",
    "dHf_kJ/mol",
    "S_J/mol/K",
    "dGf_kJ/mol",
    "source",
    "cas",
    "name",
    "charge",
    "H_kJ/mol",
    "Cp_J/mol/K",
]
_PATHWAY_SCHEMA = [
    "step",
    "equation",
    "dH_kJ/mol",
    "dG_kJ/mol",
    "cum_dH",
    "cum_dG",
    "uphill",
]


def _kj(v: float | None, nd: int = 1) -> str:
    return "unavailable" if v is None else f"{v / 1000:.{nd}f}"


def _num(v: float | None, nd: int = 1) -> str:
    return "unavailable" if v is None else f"{v:.{nd}f}"


def _source(sp: Any) -> str:
    d = sp.data
    if d.dHf is None and d.S is None:
        return "unavailable"
    tag = f"{d.source}: {d.tables}" if d.tables else d.source
    return f"{tag} (partial)" if d.missing else tag


def _species_table(r: ReactionResult) -> str:
    rows = [
        {
            "species": sp.data.formula,
            "side": sp.side,
            "coef": f"{sp.coef:g}",
            "phase": sp.data.phase,
            "dHf_kJ/mol": _kj(sp.data.dHf),
            "S_J/mol/K": _num(sp.data.S, 2),
            "dGf_kJ/mol": _kj(sp.data.dGf),
            "source": _source(sp),
            "cas": sp.data.cas or "unavailable",
            "name": sp.data.name or "identity unavailable",
            "charge": str(sp.data.charge)
            if sp.data.charge is not None
            else "unavailable",
            "H_kJ/mol": _kj(sp.data.H),
            "Cp_J/mol/K": _num(sp.data.Cp, 2),
        }
        for sp in r.species
    ]
    return render_agent_table(rows, schema=_SPECIES_SCHEMA)


def _reaction_lines(r: ReactionResult) -> list[str]:
    lines = [
        f"ΔH = {_kj(r.dH)} kJ/mol",
        f"ΔS = {_num(r.dS, 2)} J/mol/K",
        f"ΔG({r.T:g} K) = {_kj(r.dG)} kJ/mol",
    ]
    if r.n_electrons is not None:
        e = "unavailable (needs ΔG)" if r.E is None else f"{r.E:.2f} V"
        lines.append(f"E° = {e}  (n = {r.n_electrons:g}, F = {F_CONST} C/mol)")
    up = r.uphill
    if up is not None and up[0]:
        basis = "ΔG > 0" if up[1] == "ΔG" else "ΔH > 0 (ΔG unavailable)"
        lines.append(f"UPHILL: {basis}")
    lines += [f"⚠ {u}" for u in r.unavailable]
    lines += [f"note: {n}" for n in r.notes]
    return lines


def _render_one(r: ReactionResult) -> str:
    head = f"# reaction energetics at {r.T:g} K\n{r.equation}  (balanced)"
    return "\n".join([head, _species_table(r), *_reaction_lines(r)])


def _render_pathway(p: PathwayLedger) -> str:
    rows = []
    for i, (s, ch, cg) in enumerate(zip(p.steps, p.cum_dH, p.cum_dG, strict=True), 1):
        up = s.uphill
        rows.append(
            {
                "step": i,
                "equation": s.equation,
                "dH_kJ/mol": _kj(s.dH),
                "dG_kJ/mol": _kj(s.dG),
                "cum_dH": _kj(ch),
                "cum_dG": _kj(cg),
                "uphill": "—" if up is None else (f"yes ({up[1]})" if up[0] else "no"),
            }
        )
    out = [
        f"# pathway energetics at {p.T:g} K ({len(p.steps)} steps)",
        render_agent_table(rows, schema=_PATHWAY_SCHEMA),
    ]
    for i, s in enumerate(p.steps, 1):
        out += [f"step {i}: {s.equation}", _species_table(s)]
        out += [f"⚠ step {i}: {u}" for u in s.unavailable]
    out += [f"note: {n}" for n in sorted({n for s in p.steps for n in s.notes})]
    out.append(
        "uphill = ΔG > 0 (ΔH > 0 where ΔG is unavailable); cumulative values "
        "are unavailable from the first step that lacks the quantity."
    )
    return "\n".join(out)


def _float_arg(name: str, v: Any, default: float | None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadInput(f"{name}={v!r} must be a number", next=_NEXT) from exc


def render_energetics(
    q: str | None, T: Any = None, n_electrons: Any = None
) -> Response:
    """Entry point for ``view='energetics'``."""
    if q is None or not str(q).strip():
        raise BadInput(
            "view='energetics' needs q='<equation>' "
            "(several separated by ';' = a pathway)",
            next=_NEXT,
        )
    eqs = [e.strip() for e in re.split(r"[;\n]", str(q)) if e.strip()]
    t = _float_arg("T", T, 298.15)
    assert t is not None
    n = _float_arg("n_electrons", n_electrons, None)
    if len(eqs) == 1:
        return Response(
            body=_render_one(reaction_energetics(eqs[0], T=t, n_electrons=n))
        )
    if n_electrons is not None:
        raise BadInput(
            "n_electrons is supported for a single reaction only; supply it per step in separate calls",
            next=_NEXT,
        )
    return Response(body=_render_pathway(pathway_ledger(eqs, T=t)))
