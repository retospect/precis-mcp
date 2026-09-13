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

import math
import re
from dataclasses import dataclass, field

from precis_chem.ir import RouteGraph, RouteStep

#: Vacuum permittivity, F/m.
EPS_0 = 8.8541878128e-12

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


def charge_relaxation_frequency_hz(sigma_s_per_m: float, eps_r: float) -> float:
    """The droplet's Maxwell–Wagner charge-relaxation frequency, Hz.

    ``f_c = σ / (2π εr ε0)``. Below ``f_c`` the liquid relaxes charge faster
    than the field alternates, so it behaves as a **conductor**: the whole
    potential drop lands across the dielectric layer and electrowetting gets
    its full force. Above ``f_c`` the field penetrates the bulk, the voltage
    divides between droplet and dielectric, and the force decays toward the
    liquid-dielectrophoresis limit — the droplet stops responding to EWOD.

    So this is not a device parameter: it is a property of **the reaction
    medium**, and it moves when the chemistry changes the ionic strength.
    """
    if eps_r <= 0:
        raise ValueError("eps_r must be positive")
    return sigma_s_per_m / (2.0 * math.pi * eps_r * EPS_0)


#: Order-of-magnitude conductivity/permittivity for candidate droplet media —
#: handbook values, used only for the advisory verdict below, never asserted
#: as a claim. Conductivity of a nominally pure organic is purity-dominated
#: and spans decades; that is the point being made, not a number to cite.
DROPLET_MEDIA: dict[str, tuple[float, float]] = {
    # name: (sigma S/m, eps_r)
    "water, ultrapure": (5.5e-6, 80.0),
    "water, deionized (CO2-equilibrated)": (1e-4, 80.0),
    "aqueous buffer, 10 mM": (0.12, 80.0),
    "aqueous buffer, 100 mM / PBS": (1.2, 80.0),
    "acetonitrile, neat": (1e-8, 37.5),
    "acetonitrile, 0.1 M supporting electrolyte": (1.5, 37.5),
    "toluene": (1e-12, 2.4),
}


def polarization_regime(
    sigma_s_per_m: float, eps_r: float, f_actuation_hz: float
) -> str:
    """``electrowetting`` / ``marginal`` / ``dielectrophoretic`` at ``f``.

    ``marginal`` is the decade bracketing ``f_c`` — where the force is
    falling but not gone, and where a small chemistry-driven change in ionic
    strength flips the behaviour. A medium that lands there is not usable
    for a reproducible multi-step route even though single droplets may move.
    """
    f_c = charge_relaxation_frequency_hz(sigma_s_per_m, eps_r)
    if f_actuation_hz <= f_c / 3.0:
        return "electrowetting"
    if f_actuation_hz <= f_c * 3.0:
        return "marginal"
    return "dielectrophoretic"


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
    #: ``term → reason`` — conditions that put the droplet's charge-relaxation
    #: frequency near or below the actuation band, so it stops responding to
    #: electrowetting (see :func:`polarization_regime`). Kept separate from
    #: ``hazard_terms`` because the remedy is different in kind: a hazard says
    #: "this step can't run here", a polarization flag says "this step needs a
    #: supporting electrolyte it didn't ask for, which is a chemistry change".
    polarization_terms: tuple[tuple[str, str], ...] = field(default=())
    #: Practical AC actuation band in Hz (low, high). The low end is set by
    #: electrolysis (DC and near-DC drive Faradaic damage), the high end by
    #: the droplet's own ``f_c``. A medium whose ``f_c`` falls below the low
    #: end has an **empty** window — no frequency both avoids electrolysis
    #: and keeps the droplet in the electrowetting regime.
    actuation_band_hz: tuple[float, float] | None = None
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
        "the droplet must POLARIZE faster than the field alternates: "
        "f_c = σ/(2π εr ε0) must sit above the actuation band, else the "
        "liquid acts as a dielectric and EWOD force collapses. Neat "
        "acetonitrile is f_c ≈ 5 Hz and ultrapure water ≈ 1 kHz — both at "
        "or below a practical 100 Hz–10 kHz band — so the platform FORCES a "
        "supporting electrolyte into the droplet phase (~0.1 M). That is a "
        "reagent the chemistry did not choose: it shifts ionic strength, "
        "adds a counter-ion that may coordinate or compete, is usually "
        "hygroscopic (fighting anhydrous steps), and cannot be removed "
        "on-chip",
        "conductivity DRIFTS along a route — a step that consumes ions, "
        "chelates them, or precipitates a salt lowers σ and can push a "
        "droplet out of the actuation regime partway through a synthesis "
        "that started fine",
        "no vigorous gas evolution (bubbles pin at the contact line and "
        "break actuation)",
        "NO SEPARATIONS: no filtration, chromatography, distillation, or "
        "phase split except partitioning into the filler oil. Inter-step "
        "purification is unavailable, so conversions multiply and side "
        "products accumulate over a multi-step route",
        "precipitates/solids only at low loading — dense slurries stall "
        "transport and foul the surface",
        "droplet volume is quantized by electrode area × gap height, and a "
        "reliable split needs ≈2:1 — so stoichiometry comes in integer unit "
        "droplets; a non-integer ratio costs a dilution series",
        "the device WEARS: dielectric charge trapping drifts the threshold "
        "voltage up over cycles, and hydrophobic/proteinaceous adsorption "
        "kills a spot's hydrophobicity irreversibly (the droplet pins "
        "there). Both scale with step count",
        "no inert atmosphere — silicone oil dissolves O2 readily, so "
        "air- and moisture-sensitive chemistry is compromised",
        "in-situ observation is optical only (fluorescence/imaging); no "
        "NMR/IR/MS without taking material off-chip, and there is little "
        "material to take",
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
        # Separations: the platform does liquid handling, not unit operations.
        ("chromatography", "no on-chip chromatography — purify off-chip"),
        ("chromatograph", "no on-chip chromatography — purify off-chip"),
        # Same reason string as "chromatography" above, deliberately: the
        # dedup in screen_step keys on the reason, so sharing it makes
        # "column chromatography" report one finding instead of two.
        ("column", "no on-chip chromatography — purify off-chip"),
        ("recrystallize", "no mother-liquor removal on chip"),
        ("recrystallise", "no mother-liquor removal on chip"),
        ("extraction", "no phase split on chip except into the filler oil"),
        ("extract", "no phase split on chip except into the filler oil"),
        ("wash", "no phase split on chip except into the filler oil"),
        ("separatory", "no phase split on chip except into the filler oil"),
        ("inert atmosphere", "silicone oil dissolves O2 — no inert blanket"),
        ("under argon", "silicone oil dissolves O2 — no inert blanket"),
        ("under nitrogen", "silicone oil dissolves O2 — no inert blanket"),
        ("glovebox", "silicone oil dissolves O2 — no inert blanket"),
    ),
    polarization_terms=(
        (
            "anhydrous",
            "an anhydrous droplet is low-σ unless something ionic is "
            "dissolved in it — confirm the medium carries enough electrolyte "
            "to actuate (in ¹⁸F chemistry the K222/K⁺ or TBA⁺ phase-transfer "
            "agent already serves as one; a neat dry solvent does not), and "
            "note that such salts are hygroscopic",
        ),
        (
            "deionized water",
            "deionized water is f_c ≈ 1–20 kHz, at or inside the actuation "
            "band — buffer or salt it to actuate reproducibly",
        ),
        (
            "di water",
            "deionized water is f_c ≈ 1–20 kHz, at or inside the actuation "
            "band — buffer or salt it to actuate reproducibly",
        ),
        (
            "ultrapure water",
            "ultrapure water is f_c ≈ 1 kHz — inside the actuation band; "
            "add electrolyte",
        ),
        (
            "distilled water",
            "distilled water has low σ — add electrolyte to actuate",
        ),
        (
            "salt-free",
            "the platform requires a supporting electrolyte to actuate at all",
        ),
        (
            "salt free",
            "the platform requires a supporting electrolyte to actuate at all",
        ),
        (
            "electrolyte-free",
            "the platform requires a supporting electrolyte to actuate at all",
        ),
        (
            "desalt",
            "desalting removes the conductivity the platform needs to actuate",
        ),
        (
            "ion exchange",
            "ion exchange removes the conductivity the platform needs",
        ),
        (
            "low ionic strength",
            "low ionic strength lowers f_c toward the actuation band",
        ),
    ),
    # Low end set by electrolysis at/near DC, high end by practice.
    actuation_band_hz=(100.0, 10_000.0),
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
    # Several spellings map to one reason ("chromatography"/"column",
    # "extract"/"wash"/"extraction"). Report the finding once — a step that
    # says "column chromatography" has one problem, not two.
    seen_reasons: set[str] = set()
    for term, reason in (*constraint.hazard_terms, *constraint.polarization_terms):
        if reason in seen_reasons:
            continue
        if _word_re(term).search(text):
            seen_reasons.add(reason)
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


def medium_verdict(constraint: PlatformConstraint, medium: str) -> str:
    """Is ``medium`` (a :data:`DROPLET_MEDIA` key) actuatable on ``constraint``?

    Evaluates the whole actuation band, not one frequency: the band's LOW end
    is the most favourable frequency available (electrolysis sets the floor),
    so if the medium is already dielectrophoretic there, no usable frequency
    exists and the window is empty.
    """
    if medium not in DROPLET_MEDIA:
        raise ValueError(f"unknown medium {medium!r}; known: {sorted(DROPLET_MEDIA)}")
    if constraint.actuation_band_hz is None:
        return f"{medium}: no actuation band declared for {constraint.name}"
    sigma, eps_r = DROPLET_MEDIA[medium]
    low, high = constraint.actuation_band_hz
    f_c = charge_relaxation_frequency_hz(sigma, eps_r)
    at_low = polarization_regime(sigma, eps_r, low)
    at_high = polarization_regime(sigma, eps_r, high)
    head = f"{medium}: f_c ≈ {f_c:.3g} Hz vs band {low:g}–{high:g} Hz — "
    if at_low == "dielectrophoretic":
        return head + (
            "EMPTY WINDOW: dielectrophoretic even at the band floor, so no "
            "frequency both avoids electrolysis and actuates. Needs a "
            "supporting electrolyte (a chemistry change)."
        )
    if at_high == "electrowetting":
        return head + "ok: electrowetting across the whole band."
    return head + (
        f"marginal: electrowetting near {low:g} Hz but {at_high} by "
        f"{high:g} Hz — actuation depends on ionic strength holding up."
    )


def requirements_render(names: list[str]) -> str:
    """The requirement ledger rendered under a constrained route."""
    lines: list[str] = []
    for c in resolve_constraints(names):
        lines.append(f"## constraint: {c.name} — {c.title}")
        lines += [f"- {req}" for req in c.requirements]
    return "\n".join(lines)
