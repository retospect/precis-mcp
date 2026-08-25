"""Kinetics panel data for the pathway detail page.

A pathway ref stores the raw ``kinetics.solve`` record under
``meta.results.kinetics`` (folded in by ``precis_pathway.runner.run_kinetics``).
The page shows the same panel the catpath run report shows, so this module is
a port of the engine's report-side pair — the payload trim and the rule-based
plain-language verdict (``autocatpath/report.py``, ``_kinetics_payload`` /
``_kinetics_verdict``). The thresholds and sentence templates here must track
the engine's: when catpath's panel changes, re-sync this module and the
companion renderer ``static/pathway-kinetics.js`` rather than letting the two
surfaces describe the same record differently.

Same division of labour as the report: verdict in Python so the thresholds
are unit-testable, payload a TRIM not a transform (the page never
re-derives), all HTML built client-side by the vendored panel script.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: TOF below which the sign is numerical noise, not a direction: one turnover
#: per site per ~three years. Under it the verdict says "does not turn over"
#: rather than reading a direction out of the last digits.
_TOF_NOISE = 1e-8


def _species(state: str) -> list[str]:
    """Bare species of a state name — ``"NO@N~n2"`` -> ``NO``."""
    return [p.split("@")[0].split("~")[0] for p in str(state).split("+") if p]


def _fin(x: object) -> float:
    """A kinetics number that may be PRESENT AND NULL (``json_safe`` writes
    non-finite floats as ``null``); missing is NaN here, never a substitute."""
    return float(x) if isinstance(x, int | float) and math.isfinite(x) else float("nan")


def _dict(x: object) -> dict[str, Any]:
    """A record sub-field that should be a mapping — ``{}`` on any other
    shape. Unlike the report (which renders its own run folder), this reads
    stored records of every vintage: a drifted shape must degrade the panel,
    never 500 the pathway page."""
    return x if isinstance(x, dict) else {}


def _strlist(x: object) -> list[str]:
    """The warnings field as a list of strings — anything else (including a
    bare string, which would otherwise iterate per character) is []."""
    return [w for w in x if isinstance(w, str)] if isinstance(x, list) else []


def kinetics_verdict(d: dict[str, Any]) -> dict[str, Any]:
    """Plain-language verdict, synthesised from the kinetics record by FIXED
    RULES — thresholds and sentence templates, no model in the loop. Every
    clause is licensed by a number the panel also prints: a reading aid for
    the tables below it, never evidence of its own."""
    warns = _strlist(d.get("warnings"))
    tof = _fin(d.get("tof"))  # NaN when absent or written-through null
    br = _dict(d.get("tof_bracket"))
    # a null coverage is an ABSENT number, not a small one: drop it rather
    # than let max() compare it against a float
    cov = {
        k: v
        for k, v in _dict(d.get("coverages")).items()
        if isinstance(v, int | float) and math.isfinite(v)
    }
    product = d.get("product") or "the product"
    finite = math.isfinite(tof)
    # 1. trust gate. An excluded step that is load-bearing means the TOF is a
    #    bracket, not a value — that outranks anything the number would say.
    untrusted = bool(br) and not br.get("agree", True)
    if untrusted:
        missing = [
            str(s) for s in (br.get("load_bearing") or br.get("bounded_steps") or [])
        ]
        n = len(missing)
        head = (
            "Not determined: the excluded-step bracket does not agree."
            if not n
            else f"Not determined: {n} step{'' if n == 1 else 's'} with no computed barrier change{'s' if n == 1 else ''} the answer."
        )
        lines = [
            "The TOF lies between {:.3g} and {:.3g} /site/s (slow limit = the "
            "step never happens, fast limit = its optimistic bound). "
            "Missing: {}. Everything below is provisional until those "
            "barriers are computed.".format(
                _fin(br.get("tof_slow")),
                _fin(br.get("tof_fast")),
                ", ".join(missing) or "(not individually attributed)",
            )
        ]
    # 2. otherwise read the magnitude, and only then the direction
    elif not finite:
        head, lines = "No turnover number: the solve did not return a TOF.", []
    elif abs(tof) < _TOF_NOISE:
        head = (
            f"Does not turn over: TOF = {tof:.3g} /site/s, indistinguishable from zero."
        )
        lines = []
    elif tof < 0:
        head = (
            f"Runs backwards: net {product} consumption at {-tof:.3g} /site/s, so the "
            "stated product pressure is high enough to drive the cycle in "
            "reverse."
        )
        lines = []
    else:
        band = (
            "essentially inactive"
            if tof < 1e-6
            else "slow but finite"
            if tof < 1e-2
            else "active"
            if tof < 1e2
            else "fast"
        )
        head = f"Turns over, {band}: TOF = {tof:.3g} /site/s."
        lines = []
    # 3. what the surface is doing — the dominant coverage, named
    if cov:
        state, theta = max(cov.items(), key=lambda kv: kv[1])
        if theta >= 0.5 and product in _species(state):
            lines.append(
                f"Product-inhibited: the surface sits at θ({state}) = {theta:.3f}, so "
                f"{product} is not leaving it."
            )
        elif theta >= 0.5:
            lines.append(
                f"The surface is saturated in {state} (θ = {theta:.3f}) -- that is the "
                "most abundant reaction intermediate, and nothing else holds "
                "appreciable coverage."
            )
        else:
            lines.append(
                f"No single intermediate dominates (largest is {state} at "
                f"θ = {theta:.3f})."
            )
    # 3b. selectivity — does anything besides the target leave the surface?
    prod = {
        k: v
        for k, v in (_dict(d.get("production"))).items()
        if isinstance(v, int | float) and math.isfinite(v)
    }
    side = sorted(
        ((k, v) for k, v in prod.items() if k != product and v > _TOF_NOISE),
        key=lambda kv: -kv[1],
    )
    sel = d.get("selectivity")
    if side:
        one = len(side) == 1
        side_txt = ", ".join(f"{k} at {v:.3g} /site/s" for k, v in side)
        lines.append(
            f"Not fully selective: side product{'' if one else 's'} "
            f"{side_txt} also leave{'s' if one else ''} the surface"
            + (
                f" -- {100 * sel:.0f}% of net gas production is {product}."
                if isinstance(sel, int | float) and math.isfinite(sel)
                else "."
            )
        )
    # 4. what controls it
    drc = _dict(_dict(d.get("drc")).get("X_RC"))
    rc = sorted(
        (
            (k, v)
            for k, v in drc.items()
            if isinstance(v, int | float) and math.isfinite(v)
        ),
        key=lambda kv: -abs(kv[1]),
    )
    if rc and abs(rc[0][1]) > 2.0:
        # X_RC is a normalised sensitivity: single steps live in 0..1 and the
        # set sums to ~1. Decades outside that is not a strong dependence, it
        # is an ill-conditioned solve, and must not be read as a ranking.
        lines.append(
            f"Rate control is UNUSABLE here: the largest X_RC is {rc[0][0]} "
            f"at {rc[0][1]:+.2f}, far outside the physical 0-1 range, which "
            "means the linearisation the sensitivity is taken about "
            "is not a well-conditioned steady state."
        )
    elif rc and abs(rc[0][1]) >= 0.5:
        lines.append(f"Rate control sits on {rc[0][0]} (X_RC = {rc[0][1]:+.2f}).")
    elif rc:
        lines.append(
            "Rate control is shared, no step above 0.5: {}.".format(
                ", ".join(f"{k} ({v:+.2f})" for k, v in rc[:3])
            )
        )
    trc = _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC"))
    brakes = sorted(
        (
            (k, v)
            for k, v in trc.items()
            if isinstance(v, int | float) and math.isfinite(v) and v <= -0.5
        ),
        key=lambda kv: kv[1],
    )
    if brakes:
        lines.append(
            f"Strongest thermodynamic brake: {brakes[0][0]} (X_TRC = {brakes[0][1]:+.2f}) -- "
            "stabilising it further slows the cycle."
        )
    # 5. caveats the numbers alone do not carry
    caveats = []
    nullity = _dict(d.get("steady_state")).get("nullity")
    if isinstance(nullity, int) and nullity > 1:
        caveats.append(
            f"the network has {nullity} absorbing components, so these coverages and "
            f"the TOF are the ODE at t = {_fin(_dict(d.get('steady_state')).get('t_end')):g} s from one starting coverage, not a "
            f"unique steady state"
        )
    # both wordings survive: pre-0.18 records say "for the product X", newer
    # ones warn per gas ("for X; its gas exchange ...")
    defaulted = [
        m.group(1)
        for w in warns
        for m in [re.search(r"no pressure stated for (?:the product )?(\S+?)[;,]", w)]
        if m
    ]
    if defaulted:
        gases = ", ".join(dict.fromkeys(defaulted))
        which = "those" if len(defaulted) > 1 else "its"
        caveats.append(
            f"p({gases}) was never stated, so the 1 bar reference is "
            f"doing the work in {which} gas-exchange rates"
        )
    n_mismatch = sum(1 for w in warns if "differs from its endpoints" in w)
    if n_mismatch:
        caveats.append(
            f"""{n_mismatch} step{"'s" if n_mismatch == 1 else "s'"} aggregated ΔE disagrees with its endpoint energies by more than 0.05 eV"""
        )
    return {
        "headline": head,
        "lines": lines,
        "caveats": caveats,
        "tone": (
            "dead"
            if untrusted
            else "warn"
            if not finite or abs(tof) < _TOF_NOISE or tof < 0
            else "ok"
        ),
    }


def kinetics_payload(results: dict[str, Any]) -> dict[str, Any] | None:
    """What the panel shows, keyed by tier (``ml``/``dft``) — the shape the
    vendored panel script expects. A trim, not a transform. ``None`` when the
    run recorded no kinetics (older pathways, kinetics-less slices) — the
    page then omits the panel entirely rather than rendering an empty shell.

    Differences from the report's trim, both because the web page renders a
    stored record rather than a run folder: the input is ``meta.results``
    (one record, tier named inside it) instead of two side-car files, and
    there is no ``file`` link for the tier table's record column.
    """
    d = results.get("kinetics")
    if not isinstance(d, dict) or "tof" not in d:
        return None
    cov = _dict(d.get("coverages"))
    sens = _dict(d.get("sensitivity"))
    br = _dict(d.get("tof_bracket")) or None
    warns = _strlist(d.get("warnings"))
    trans = d.get("transitions")
    tier = "dft" if d.get("tier") == "dft" else "ml"
    return {
        tier: {
            "bracket": (
                {
                    k: br.get(k)
                    for k in (
                        "tof_slow",
                        "tof_fast",
                        "load_bearing",
                        "bounded_steps",
                        "agree",
                    )
                }
                if br
                else None
            ),
            "conditions": d.get("conditions"),
            "product": d.get("product"),
            "tof": d.get("tof"),
            "production": _dict(d.get("production")),
            "selectivity": d.get("selectivity"),
            "span_eV": d.get("span_eV"),
            "tof_span_limit": d.get("tof_span_limit"),
            "coverage_effect": d.get("coverage_effect"),
            "mari_seed": d.get("mari_seed"),
            "coverages": dict(list(cov.items())[:8]),
            # how many there were BEFORE the slice: the page may only claim a
            # remainder exists when one actually does
            "n_coverages": len(cov),
            "drc": _dict(_dict(d.get("drc")).get("X_RC")),
            "trc": _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC")),
            "sensitivity": {
                k: sens.get(k) for k in ("tof", "n_samples", "controlling")
            },
            "warnings": warns,
            "n_warnings": len(warns),
            # every transition of the master equation, in solver order: the
            # page prints the concrete ODE system, the numbered rate-constant
            # table, and the per-step arithmetic behind X_RC from these
            "transitions": [
                {
                    k: t.get(k)
                    for k in (
                        "name",
                        "from",
                        "to",
                        "kind",
                        "dG_eV",
                        "barrier_eV",
                        "k_f",
                        "k_b",
                        "net_rate",
                    )
                }
                for t in (trans if isinstance(trans, list) else [])
                if isinstance(t, dict)
            ],
            "verdict": kinetics_verdict(d),
        }
    }
