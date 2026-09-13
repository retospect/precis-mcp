"""Platform constraints for the ``route`` kind — declared at ``put``,
screened advisorily over the planned graph.

A *platform constraint* names the execution substrate a synthesis is
planned for (slice 1: ``ewod-oil`` — EWOD digital microfluidics with an
oil filler medium). Engines (AiZynthFinder / ASKCOS) cannot condition
their search on reaction media, so a constraint here is honest about
what it is: a **declared requirement recorded on the route + an
advisory per-step screen**, never a filter the planner obeyed. The
declared set is part of the content address (``cache_key``), so the
same target planned under different constraints is a different cached
plan.

The screen is lexical over each step's free-text ``conditions`` (the
only condition signal the IR carries). Three honest outcomes per step:

* ``unscreened`` — the engine reported no conditions; nothing to check.
* ``check: …`` — a term in the conditions matches a known
  incompatibility (a deny-listed solvent, a hazard keyword, a
  temperature outside the platform's envelope).
* ``ok: …`` — an allow-listed droplet solvent named, no hazard hit.

Solvent and hazard terms match on **word boundaries**, not bare
substrings: a reagent whose name merely contains a solvent name is not a
solvent. ``p-toluenesulfonic acid`` is not toluene, ``cyclohexane`` is
not hexane, and ``acetohydroxamic acid`` is not ethanol — all three
false-flagged under substring matching. Temperatures are parsed
numerically against the platform's envelope rather than keyword-matched,
because ``150 °C`` / ``150°C`` / ``150 C`` are the same hazard and a
keyword list catches only the spelling it happens to contain.

Pure Python, zero chemistry deps — imports clean on the request path
(same rule as :mod:`precis_chem.ir`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from precis_chem.ir import RouteGraph, RouteStep

#: Celsius temperatures in a conditions string: an optional sign, digits,
#: optional decimal, optional space/degree-sign, then a ``C`` that ends a
#: word. Matches ``80 °C`` / ``150°C`` / ``-78 C`` / ``25°c`` alike. The
#: trailing ``\b`` after ``[cC]`` is what keeps ``Cs2CO3``, ``CaCl2`` and a
#: ``C18`` column from reading as temperatures. Kelvin and Fahrenheit are
#: deliberately not parsed — an unrecognised unit must fall through to
#: ``unscreened`` rather than be silently rescaled.
_TEMP_C_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:°|deg(?:rees)?\s*)?[cC]\b")


def _word_re(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for one lexicon term (may contain spaces)."""
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


@dataclass(frozen=True, slots=True)
class PlatformConstraint:
    """One named execution-platform constraint the route declares."""

    name: str
    title: str
    #: The requirement ledger rendered under a constrained route — each line
    #: one condition the platform imposes on every step.
    requirements: tuple[str, ...]
    #: Droplet-phase solvents known actuatable on the platform (lowercase,
    #: word-boundary matched against the step's conditions text).
    solvent_allow: tuple[str, ...] = field(default=())
    #: Solvents/media excluded by the platform (merge with or partition into
    #: the filler oil, or non-actuatable).
    solvent_deny: tuple[str, ...] = field(default=())
    #: ``term → reason`` — condition-text keywords flagging an operation the
    #: platform cannot host (kept as pairs; dict-in-frozen-dataclass is
    #: unhashable). Temperature limits do NOT belong here — use the
    #: ``temp_*_c`` bounds, which are parsed numerically.
    hazard_terms: tuple[tuple[str, str], ...] = field(default=())
    #: Droplet thermal envelope in °C. A parsed temperature outside
    #: ``[temp_min_c, temp_max_c]`` raises a ``check``. ``None`` disables
    #: that side of the bound.
    temp_min_c: float | None = None
    temp_max_c: float | None = None


#: EWOD digital microfluidics, oil filler medium (silicone oil; fluorinated
#: oils like FC-75 dissolve Teflon AF and are out for standard stacks).
#:
#: Provenance of the numbers below: a 2026-09-13 secondary literature survey
#: (Perplexity Sonar deep-research), whose named primaries are Pollack's
#: filler-fluid miscibility tests, the Cytop/Teflon-AF composite breakdown
#: measurements, and EWOD ¹⁸F-radiochemistry practice at 80–120 °C. **None of
#: those primaries is held in the precis corpus** as of that date, so these
#: lines are survey-grade, not citation-grounded, and no claim hub asserts
#: them — treat a flag as review guidance and re-derive a number from the
#: primary before it enters a publishable claim. Acquire the primaries and
#: this docstring should name them by handle instead.
EWOD_OIL = PlatformConstraint(
    name="ewod-oil",
    title="EWOD digital microfluidics, oil filler",
    requirements=(
        "droplet phase must be immiscible with the silicone-oil filler: "
        "in practice water/aqueous mixtures and acetonitrile only — "
        "silicone oil mixes with nearly all other common organics (THF, "
        "DCM, ethanol, methanol, DMF, ethers, hydrocarbons), which "
        "dissolve as droplets; nonpolar solvents also actuate poorly "
        "(low permittivity)",
        "fluorinated filler oils (FC-75) are immiscible with almost "
        "everything but dissolve Teflon AF — incompatible with standard "
        "fluoropolymer stacks",
        "temperature window ≈ room temp to 100–120 °C (fluoropolymer "
        "dielectric + oil stability; >150 °C avoided) — no reflux, no "
        "distillation, no cryogenic steps",
        "actuation ≈ 20–80 V on ~0.5–0.7 µm Cytop/Teflon-AF stacks; "
        "strongly ionic media need AC actuation to avoid electrolysis",
        "no vigorous gas evolution (bubbles pin at the contact line and "
        "break actuation)",
        "precipitates/solids only at low loading — no on-chip filtration; "
        "dense slurries stall transport and foul the surface",
        "hydrophobic solutes partition into the oil — reagent loss and "
        "droplet-to-droplet cross-contamination scale with residence time",
        "surfactants alter interfacial tension — keep below the "
        "concentration that emulsifies or defeats electrowetting",
    ),
    solvent_allow=(
        "water",
        "aqueous",
        "h2o",
        "acetonitrile",
        "mecn",
        "ch3cn",
        "brine",
        "buffer",
    ),
    solvent_deny=(
        "thf",
        "tetrahydrofuran",
        "dcm",
        "dichloromethane",
        "chloroform",
        "chcl3",
        "methanol",
        "meoh",
        "ethanol",
        "etoh",
        "isopropanol",
        "dmf",
        "dimethylformamide",
        "dmso",
        "dimethyl sulfoxide",
        "diethyl ether",
        "et2o",
        "ethyl acetate",
        "etoac",
        "hexane",
        "pentane",
        "heptane",
        "toluene",
        "benzene",
        "xylene",
        "dioxane",
        "petroleum ether",
    ),
    hazard_terms=(
        ("reflux", "reflux exceeds the on-chip thermal envelope"),
        ("distill", "distillation is not performable in a droplet"),
        ("distillation", "distillation is not performable in a droplet"),
        ("filtration", "on-chip filtration of solids is unavailable"),
        ("filter", "on-chip filtration of solids is unavailable"),
        ("gas evolution", "gas evolution breaks droplet actuation"),
        ("hydrogen gas", "gas evolution/consumption breaks droplet actuation"),
        ("h2 gas", "gas evolution/consumption breaks droplet actuation"),
        ("sparge", "gas sparging breaks droplet actuation"),
        ("autoclave", "sealed-vessel pressure is not available on chip"),
        ("solvothermal", "solvothermal conditions exceed the thermal envelope"),
    ),
    # Fluoropolymer dielectric + silicone-oil stability set the ceiling;
    # below 0 °C the filler oil's viscosity stalls droplet transport.
    temp_min_c=0.0,
    temp_max_c=120.0,
)

#: Registry of known platform constraints by name.
_CONSTRAINTS: dict[str, PlatformConstraint] = {EWOD_OIL.name: EWOD_OIL}


def resolve_constraints(names: object) -> list[PlatformConstraint]:
    """Resolve a caller-supplied constraint spec to registry entries.

    Accepts a list/tuple of names or one comma-separated string; empty/None
    ⇒ ``[]``. Unknown names raise ``ValueError`` naming the known set.
    """
    if names is None:
        return []
    raw: list[str]
    if isinstance(names, str):
        raw = [p.strip() for p in names.split(",")]
    elif isinstance(names, (list, tuple)):
        raw = [str(p).strip() for p in names]
    else:
        raise ValueError(
            f"constraints must be a name list or comma string, got {type(names).__name__}"
        )
    out: list[PlatformConstraint] = []
    seen: set[str] = set()
    for name in raw:
        if not name:
            continue
        key = name.lower()
        c = _CONSTRAINTS.get(key)
        if c is None:
            raise ValueError(
                f"unknown platform constraint {name!r}; known: {sorted(_CONSTRAINTS)}"
            )
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _temperatures_c(text: str) -> list[float]:
    """Every Celsius temperature the conditions string states."""
    return [float(m.group(1)) for m in _TEMP_C_RE.finditer(text)]


def screen_step(step: RouteStep, constraint: PlatformConstraint) -> list[str]:
    """Advisory flags for one step under one constraint (lexical, honest).

    Deny/hazard/temperature findings are checked first and suppress the
    ``ok`` branch, so a coincidental allow-term match can never mask a real
    incompatibility ("water/THF mixture" is a `check`, not an `ok`).
    """
    text = (step.conditions or "").lower()
    if not text.strip():
        return [f"{constraint.name}: unscreened — engine reported no conditions"]
    flags: list[str] = []
    for solvent in constraint.solvent_deny:
        if _word_re(solvent).search(text):
            flags.append(
                f"{constraint.name}: check — solvent '{solvent}' is "
                "incompatible with the oil filler"
            )
    for term, reason in constraint.hazard_terms:
        if _word_re(term).search(text):
            flags.append(f"{constraint.name}: check — {reason}")
    for temp in _temperatures_c(text):
        if constraint.temp_max_c is not None and temp > constraint.temp_max_c:
            flags.append(
                f"{constraint.name}: check — {temp:g} °C is above the "
                f"platform ceiling ({constraint.temp_max_c:g} °C)"
            )
        elif constraint.temp_min_c is not None and temp < constraint.temp_min_c:
            flags.append(
                f"{constraint.name}: check — {temp:g} °C is below the "
                f"platform floor ({constraint.temp_min_c:g} °C)"
            )
    if not flags:
        for solvent in constraint.solvent_allow:
            if _word_re(solvent).search(text):
                flags.append(
                    f"{constraint.name}: ok — droplet solvent '{solvent}' "
                    "is actuatable in oil"
                )
                break
        else:
            flags.append(
                f"{constraint.name}: unscreened — no recognized solvent/"
                "hazard term in conditions"
            )
    return flags


def screen_route(graph: RouteGraph, names: list[str]) -> RouteGraph:
    """Return ``graph`` re-built with the declared constraints + per-step
    advisory flags. No-op (same graph back) when ``names`` is empty."""
    constraints = resolve_constraints(names)
    if not constraints:
        return graph
    steps = [
        RouteStep(
            id=s.id,
            product=s.product,
            reactants=s.reactants,
            template_id=s.template_id,
            reaction_smarts=s.reaction_smarts,
            conditions=s.conditions,
            confidence=s.confidence,
            in_stock=s.in_stock,
            constraint_flags=[flag for c in constraints for flag in screen_step(s, c)],
        )
        for s in graph.steps
    ]
    return RouteGraph(
        target=graph.target,
        engine=graph.engine,
        engine_version=graph.engine_version,
        steps=steps,
        solved=graph.solved,
        score=graph.score,
        metrics=graph.metrics,
        provenance=graph.provenance,
        constraints=[c.name for c in constraints],
    )


def requirements_render(names: list[str]) -> str:
    """The requirement ledger rendered under a constrained route."""
    lines: list[str] = []
    for c in resolve_constraints(names):
        lines.append(f"## constraint: {c.name} — {c.title}")
        lines += [f"- {req}" for req in c.requirements]
    return "\n".join(lines)
