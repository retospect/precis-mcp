"""Kinetics panel data for the pathway detail page.

A pathway ref stores the raw ``kinetics.solve`` record under
``meta.results.kinetics`` (folded in by ``precis_pathway.runner.run_kinetics``).
The panel ports the engine's report-side pair — the payload trim and
rule-based verdict (``autocatpath/report.py``, ``_kinetics_payload`` /
``_kinetics_verdict``). ``kinetics_verdict`` stays verbatim: its thresholds
and sentence templates must track the engine's, pinned by the engine golden
parity gate. When catpath's panel changes, re-sync the port and companion
renderer ``static/pathway-kinetics.js``.

``stored_kinetics_verdict`` gates a copy before calling that port and adds
source notes from the same record's explicit stamps. Stored X_RC requires
complete finite values, the engine sum tolerance and the verdict's existing
magnitude guard; X_TRC needs complete finite values but has no sum rule.
The payload uses the same predicates to gate bars. No reader recomputes or
modifies stored data.

Same division of labour as the report: verdict in Python so the thresholds
are unit-testable, payload a TRIM not a transform (the page never
re-derives), all HTML built client-side by the vendored panel script.

Warning navigation also carries stored trust scope and potential provenance.
Absent route evidence stays unlabelled; the legacy numerical tier bucket
cannot establish DFT refinement, so the solve label reads provenance separately.
"""

from __future__ import annotations

import math
import re
from typing import Any

from precis.utils.pathway_rate_control import (
    rate_control_note,
    usable_rate_control,
    usable_thermodynamic_rate_control,
)

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


def stored_kinetics_verdict(d: dict[str, Any]) -> dict[str, Any]:
    """Gate stored control on a copy, preserving the verbatim engine port."""
    gated = dict(d)
    drc = _dict(d.get("drc"))
    trc = _dict(d.get("thermodynamic_drc"))
    if not usable_rate_control(drc.get("X_RC")):
        gated["drc"] = {key: value for key, value in drc.items() if key != "X_RC"}
    if not usable_thermodynamic_rate_control(trc.get("X_TRC")):
        gated["thermodynamic_drc"] = {
            key: value for key, value in trc.items() if key != "X_TRC"
        }
    verdict = kinetics_verdict(gated)
    verdict["lines"].extend(
        [
            rate_control_note(d.get("drc_source"), drc.get("X_RC")),
            rate_control_note(
                d.get("trc_source"), trc.get("X_TRC"), thermodynamic=True
            ),
        ]
    )
    return verdict


def _v2_tof(v2: dict[str, Any]) -> float | None:
    """The D8 ranked TOF off the record — KMC median (finite, > 0), then
    MF, then chain. Mirrors ``precis_pathway._dispatch_common._ranked_tof``:
    this module ports the engine's report shape and re-derives, never
    imports, the pathway side (the ``p50``/``p5``/``p95`` KMC interval is
    left on the record — the panel has no column for it)."""
    med = _fin(_dict(v2.get("tof_kmc")).get("median"))
    if math.isfinite(med) and med > 0:
        return med
    for key in ("tof_mf", "tof_chain"):
        v = _fin(v2.get(key))
        if math.isfinite(v):
            return v
    return None


_MF_FAILED = re.compile(r"mean-field MKM failed \((.*)\)")
_ERR_PREFIX = re.compile(r"^\w*Error:\s*(?:mkm:\s*)?")


_OBS_FAILED = re.compile(r"mean-field observable (\S+) failed \((.*)\)")

#: ``mf_status.state_err`` (the ln-coverage step of the last Newton/
#: relaxation iteration) above this means the steady state did not settle —
#: the engine's own ``autocatpath.mkm._STATE_TOL``, where it warns.
#: Mirrored in ``precis_pathway.toon_views`` (the web side never imports
#: the pathway side).
_STATE_SETTLED_TOL = 1e-4


def _mf_reading(
    v2: dict[str, Any],
) -> tuple[str | None, float | None, list[dict[str, str]], str | None, bool]:
    """``(failure, state_err, failed_observables, stage, tof_near_noise)`` of
    the record's mean-field solve. ``v2['mf_status']`` (engine >= the one
    that stamps it) wins; a stored record without it falls back to parsing
    the warnings (``mean-field MKM failed (…)`` / ``mean-field observable
    <name> failed (…)`` / ``mkm: TOF … within 10x the balance residual
    noise``). ``state_err`` is ``None`` unless the solve returned but its
    state is not settled (> :data:`_STATE_SETTLED_TOL`); ``tof_near_noise``
    flags an MF TOF inside the solver's own noise floor, where its sign and
    magnitude are unresolved."""
    st = v2.get("mf_status")
    if isinstance(st, dict):
        failure = None
        if st.get("ok") is False:
            failure = _ERR_PREFIX.sub("", str(st.get("reason") or "")).strip()
            failure = failure or "unknown error"
        err = _fin(st.get("state_err"))
        state_err = err if math.isfinite(err) and err > _STATE_SETTLED_TOL else None
        listed = st.get("failed_observables")
        found: list[dict[str, str]] = [
            {"name": str(o["name"]), "reason": str(o.get("reason") or "")}
            for o in (listed if isinstance(listed, list) else [])
            if isinstance(o, dict) and o.get("name")
        ]
        stage = st.get("stage")
        return (
            failure,
            state_err,
            found,
            stage if isinstance(stage, str) else None,
            st.get("tof_near_noise") is True,
        )
    warned: str | None = None
    near_noise = False
    parsed: list[dict[str, str]] = []
    for w in _strlist(v2.get("warnings")):
        if warned is None and (m := _MF_FAILED.search(w)):
            warned = _ERR_PREFIX.sub("", m.group(1)).strip() or "unknown error"
        elif m := _OBS_FAILED.search(w):
            parsed.append({"name": m.group(1), "reason": m.group(2).strip()})
        elif "within 10x the balance residual noise" in w:
            near_noise = True
    return warned, None, parsed, None, near_noise


def _tof_sign_reading(v2: dict[str, Any]) -> dict[str, Any] | None:
    """The ``mf_status.tof_sign`` classification (gr559294, engine >= the
    tof_sign cut) as a renderable dict — ``{"class", "label", "detail"}``.
    The class→label mapping lives in ``precis_pathway.tof_sign`` (the web
    layer only renders it); ``None`` when the record carries no
    classification — ``tof_mf >= 0`` or a pre-cut record — or the pathway
    plugin is not importable in this process, so the panel degrades to
    today's reading instead of dropping the box."""
    try:
        from precis_pathway.tof_sign import (
            tof_sign,
            tof_sign_detail,
            tof_sign_label,
        )
    except Exception:
        return None
    sign = tof_sign(v2)
    if sign is None:
        return None
    return {
        "class": str(sign.get("class") or ""),
        "label": tof_sign_label(sign),
        "detail": tof_sign_detail(sign),
    }


def _v2_solve(v2: dict[str, Any], product: Any) -> dict[str, Any]:
    """The v2 solve status the panel headlines: which rung of the ranked
    ladder produced the TOF (``kmc``/``mf``/``chain``/``none``), the
    mean-field failure reason pulled from the record's warnings, whether
    the TOF is a positive steady state, the legacy-environment flag, and
    whether the target's selectivity is on the record. Re-derives
    ``precis_pathway.toon_views._v2_status_lines``'s reading (the web side
    never imports the pathway side)."""
    med = _fin(_dict(v2.get("tof_kmc")).get("median"))
    mf = _fin(v2.get("tof_mf"))
    chain = _fin(v2.get("tof_chain"))
    if math.isfinite(med) and med > 0:
        rung, tof = "kmc", med
    elif math.isfinite(mf):
        rung, tof = "mf", mf
    elif math.isfinite(chain):
        rung, tof = "chain", chain
    else:
        rung, tof = "none", None
    failure, state_err, failed_obs, stage, mf_near_noise = _mf_reading(v2)
    # the engine stamps ``tof_chain_near_noise`` beside ``tof_chain``; an
    # older record says it only in the warnings
    chain_near_noise = v2.get("tof_chain_near_noise") is True or any(
        "inside its cancellation noise" in w for w in _strlist(v2.get("warnings"))
    )
    near_noise = {"mf": mf_near_noise, "chain": chain_near_noise}.get(rung, False)
    unsettled = state_err is not None
    sel = _dict(v2.get("selectivity"))
    target = product if isinstance(product, str) and product else None
    positive = tof is not None and tof > 0
    sign = _tof_sign_reading(v2)
    if failure is not None and rung != "mf":
        head = f"mean-field solve failed: {failure}"
        head += (
            f" — showing {rung} estimate" if rung != "none" else " — no TOF estimate"
        )
    else:
        head = f"TOF from the {rung} rung" if rung != "none" else "no TOF on record"
    if tof is not None and not positive:
        head += (
            f"; {sign['label']}"
            if sign is not None
            else "; no positive steady state (TOF ≤ 0, not a rate)"
        )
    if unsettled and failure is None:
        head += f"; state not fully settled (ln-coverage step {state_err:.2g}) " + (
            "— no resolved rate" if near_noise else "— order-of-magnitude TOF"
        )
    if near_noise:
        head += (
            "; TOF inside the solver's noise floor — sign and magnitude "
            "unresolved, read it as no resolved rate"
        )
    return {
        "rung": rung,
        "tof": tof,
        "positive": positive,
        "near_noise": near_noise,
        "mf_failure": failure,
        "mf_stage": stage,
        "state_err": state_err,
        "unsettled": unsettled,
        "failed_observables": failed_obs,
        "legacy": bool(_dict(v2.get("environment")).get("legacy")),
        "bep_flux_share": (
            share if math.isfinite(share := _fin(v2.get("bep_flux_share"))) else None
        ),
        "selectivity_target": target,
        "selectivity_known": (
            None if target is None else math.isfinite(_fin(sel.get(target)))
        ),
        "tof_sign": sign,
        "headline": head,
    }


def _target(results: dict[str, Any]) -> Any:
    """The pathway's product gas under a v2-only record — the record holds
    no ``product`` field; the aggregate's score block and the sweep both
    name the target (``runner._run_kinetics_sweep`` reads it the same way)."""
    t = _dict(results.get("score")).get("target")
    if isinstance(t, str) and t:
        return t
    return _dict(results.get("kinetics_sweep")).get("target")


def _v2_overlay(v2: dict[str, Any], product: Any) -> dict[str, Any]:
    """The D8 record's shared fields folded back into the v1 solve shape
    the trim reads — the D8 reader flip done at the trim boundary, so the
    vendored script and the verdict see one record shape. The flat MF
    ``drc``/``trc`` land back under ``X_RC``/``X_TRC``; per-class coverages
    (``{site_class: {token: θ}}``) flatten to ``token@class`` state names;
    the log-space uncertainty band inverts to a linear sensitivity band;
    ``selectivity``'s per-product map reduces to the target's share; the
    resolved environment fills ``conditions``. Only non-empty values are
    emitted, except that an explicit empty control map replaces v1 values:
    an unusable new map must not resurrect an older control claim. A field
    that is absent or ``null`` never erases the v1 block's answer."""
    out: dict[str, Any] = {}
    tof = _v2_tof(v2)
    if tof is not None:
        out["tof"] = tof
    # v2 tier policy (gr514393): the record carries no ``tier``; what v1
    # split into ``kinetics.dft.json`` vs ``kinetics.json`` the record
    # reports as ``bep_flux_share`` — the fraction of target production
    # through BEP-priced (non-NEB) transitions. Zero share is the v1
    # ``dft`` case (every load-bearing barrier measured); any share means
    # predicted barriers feed the answer — ``ml``. A record that lacks
    # the field (older engine) keeps the v1 block's ``tier`` or the
    # ``ml`` default.
    share = _fin(v2.get("bep_flux_share"))
    if math.isfinite(share):
        out["tier"] = "dft" if share == 0.0 else "ml"
    drc = v2.get("drc")
    if isinstance(drc, dict):
        out["drc"] = {"X_RC": drc}
        out["drc_source"] = v2.get("drc_source")
    trc = v2.get("trc")
    if isinstance(trc, dict):
        out["thermodynamic_drc"] = {"X_TRC": trc}
        out["trc_source"] = v2.get("trc_source")
    flat: dict[str, Any] = {}
    for cls, table in _dict(v2.get("coverages")).items():
        if isinstance(table, dict):
            for tok, th in table.items():
                flat[f"{tok}@{cls}"] = th
    if flat:
        out["coverages"] = flat
    warns = _strlist(v2.get("warnings"))
    if warns:
        out["warnings"] = warns
    env = _dict(v2.get("environment"))
    if env:
        gases = _dict(env.get("gases"))
        out["conditions"] = {
            "temperature": env.get("T"),
            "pressures": {
                **_dict(gases.get("inputs")),
                **_dict(gases.get("outputs")),
                **_dict(gases.get("inerts")),
            },
        }
    sel = _dict(v2.get("selectivity"))
    if isinstance(product, str) and product:
        share = _fin(sel.get(product))
        if math.isfinite(share):
            out["selectivity"] = share
        out["product"] = product
    unc = _dict(v2.get("uncertainty"))
    if unc:
        band = {
            dst: 10.0**lv
            for src, dst in (
                ("log_tof_p50", "median"),
                ("log_tof_p5", "p5"),
                ("log_tof_p95", "p95"),
            )
            if math.isfinite(lv := _fin(unc.get(src)))
        }
        if band:
            out["sensitivity"] = {
                "tof": band,
                "n_samples": unc.get("n_samples"),
                "controlling": unc.get("controlling_step"),
            }
    if math.isfinite(span := _fin(v2.get("tof_span_limit"))):
        out["tof_span_limit"] = span
    return out


def _solve_label(
    results: dict[str, Any], validation_manifest: dict[str, Any] | None
) -> str:
    """Name only recorded provenance; zero BEP share does not establish DFT.

    The legacy panel bucket also classifies measured ML barriers as ``dft``.
    Keep that numerical/parity contract separate from the reader's label.
    The manifest names executed potentials; a requested config is not proof
    of which backend actually ran.
    """
    if _dict(results.get("kinetics")).get("tier") == "dft":
        return "DFT-refined solve"
    models = _dict(_dict(validation_manifest).get("lock")).get("mlip")
    records = (
        [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []
    )
    names = []
    backends = []
    for record in records or [{"backend": results.get("backend")}]:
        backend = record.get("backend")
        model = record.get("model")
        if isinstance(backend, str) and backend and backend != "auto":
            backends.append(backend.lower())
            name = backend
            if isinstance(model, str) and model:
                name += " " + model
            if name not in names:
                names.append(name)
    if backends and all(b == "emt" for b in backends):
        return "effective-medium potential solve"
    label = "machine-learned potential"
    return label + (" (" + ", ".join(names) + ")" if names else "") + " solve"


def kinetics_payload(
    results: dict[str, Any], *, validation_manifest: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """What the panel shows, keyed by tier (``ml``/``dft``) — the shape the
    vendored panel script expects. A trim, not a transform. ``None`` when the
    run recorded no kinetics (older pathways, kinetics-less slices) — the
    page then omits the panel entirely rather than rendering an empty shell.

    Differences from the report's trim, both because the web page renders a
    stored record rather than a run folder: the input is ``meta.results``
    (one record, tier named inside it) instead of two side-car files, and
    there is no ``file`` link for the tier table's record column.

    D8 reader flip: the v2 record (``meta.results.kinetics_v2``) answers
    first — its shared fields are folded into the v1 solve shape by
    :func:`_v2_overlay` — so a stored run whose engine stopped writing the
    v1 block still renders the panel, and a run carrying both shows the
    ranked (KMC > MF > chain) numbers rather than the chain's.

    Beyond the trim the payload also derives display metadata the stored
    record lacks: coverages re-ordered by θ and grouped back into their
    site classes (gr549757 — jsonb destroys the engine's sort), the
    rate-control bars' recorded provenance (``drc_source_recorded`` and
    ``drc_note``). The older inferred ``drc_source`` field remains for
    compatibility but never licenses a display label. A vacancy-stripped
    coverage dict feeds the verdict so an
    empty site can't win the MARI slot (gr549757).
    """
    rec = results.get("kinetics")
    d = dict(rec) if isinstance(rec, dict) else {}
    v2 = results.get("kinetics_v2")
    drc_source: str | None = None
    trc_source: str | None = None
    drc_record = d
    classes: list[dict[str, Any]] = []
    if isinstance(v2, dict):
        # the record's flat ``drc``/``trc`` are the MF solve's values when it
        # produced them and the chain's (``drc["X_RC"]``) when it could not —
        # the overlay folds them over the v1 block without saying which, so
        # the panel labels the bars' provenance off this comparison. Equal to
        # the v1 values means it IS the chain's dict folded forward.
        v1_drc = _dict(_dict(d.get("drc")).get("X_RC"))
        v1_trc = _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC"))
        product = d.get("product") or _target(results)
        d.update(_v2_overlay(v2, product=product))
        d["v2_solve"] = _v2_solve(v2, product)
        if d["v2_solve"]["mf_failure"] and not _dict(v2.get("coverages")):
            # the MF solve left no steady state: v1's coverages belong to a
            # different solve, so showing them beside the chain TOF misleads
            d["coverages"] = {}

        def _rc_source(key: str, v1_flat: dict[str, Any]) -> str | None:
            # the record's flat value is the MF solve's when it produced one
            # (``drc_mf or drc["X_RC"]`` — a failed/empty MF observable falls
            # back to the chain's dict verbatim, which compares equal to the
            # v1 block's). An empty record value leaves the v1 block's in
            # place — still the chain solve's.
            merged = _dict(v2.get(key))
            if not merged:
                return "chain" if v1_flat else None
            return "mf" if merged != v1_flat else "chain"

        drc_source = _rc_source("drc", v1_drc)
        trc_source = _rc_source("trc", v1_trc)
        if isinstance(v2.get("drc"), dict):
            drc_record = v2
        if drc_record.get("drc_source") in ("mf", "chain"):
            drc_source = drc_record["drc_source"]
        if v2.get("trc_source") in ("mf", "chain") and _dict(v2.get("trc")):
            trc_source = v2["trc_source"]
        # the v2 coverage table is per site class — each class's states sum
        # to ~1 independently, so the page groups by class rather than
        # pretending the flattened dict sums to 1 (gr549757)
        for cls, table in _dict(v2.get("coverages")).items():
            if not isinstance(table, dict):
                continue
            states = [
                [str(tok), float(th)]
                for tok, th in sorted(
                    (
                        (t, x)
                        for t, x in table.items()
                        if isinstance(x, int | float) and math.isfinite(x)
                    ),
                    key=lambda kv: -kv[1],
                )
            ]
            if states:
                classes.append({"cls": str(cls), "states": states})
        classes.sort(key=lambda c: -c["states"][0][1])
    else:
        v1_drc = _dict(_dict(d.get("drc")).get("X_RC"))
        v1_trc = _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC"))
        drc_source = "chain" if v1_drc else None
        trc_source = "chain" if v1_trc else None
    if "tof" not in d:
        return None
    cov = _dict(d.get("coverages"))
    # gr549757: nothing sorts stored coverages — jsonb reorders keys by
    # length, so slicing the dict showed the eight SHORTEST-NAMED states,
    # not the largest. Emit the table as a list ordered by θ (the template's
    # tojson would re-sort a dict's keys); the page then shows every state
    # above its display threshold instead of a fixed cap. A null coverage
    # is an absent number, not a small one — it never reaches the table.
    cov_sorted = [
        [str(k), float(v)]
        for k, v in sorted(
            (
                (k, v)
                for k, v in cov.items()
                if isinstance(v, int | float) and math.isfinite(v)
            ),
            key=lambda kv: -kv[1],
        )
    ]
    # The vacancy fix lives at this call boundary: an empty site
    # (*, or *@class under the v2 flat
    # naming) is not an intermediate and must not win the MARI slot
    # (gr549757)
    d_verdict = {
        **d,
        "coverages": {k: v for k, v in cov.items() if str(k).split("@", 1)[0] != "*"},
    }
    drc_values = _dict(_dict(d.get("drc")).get("X_RC"))
    recorded_source = drc_record.get("drc_source")
    drc_note = rate_control_note(recorded_source, drc_values)
    trc_values = _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC"))
    recorded_trc_source = d.get("trc_source")
    trc_note = rate_control_note(recorded_trc_source, trc_values, thermodynamic=True)
    verdict = stored_kinetics_verdict(d_verdict)
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
            "coverages": cov_sorted,
            # per-class tables when the v2 record carries them:
            # [{"cls", "states": [[tok, θ], ...]}, ...] — θ-desc inside each
            # class, classes ordered by their largest θ
            "coverage_classes": classes,
            "n_coverages": len(cov_sorted),
            "drc": _dict(_dict(d.get("drc")).get("X_RC")),
            "drc_source": drc_source,
            "drc_source_recorded": recorded_source,
            "drc_note": drc_note,
            "drc_usable": usable_rate_control(drc_values),
            "trc": _dict(_dict(d.get("thermodynamic_drc")).get("X_TRC")),
            "trc_source": trc_source,
            "trc_source_recorded": recorded_trc_source,
            "trc_note": trc_note,
            "trc_usable": usable_thermodynamic_rate_control(trc_values),
            "sensitivity": {
                k: sens.get(k) for k in ("tof", "n_samples", "controlling")
            },
            "warnings": warns,
            "n_warnings": len(warns),
            # Display-only evidence: never infer route membership from a
            # warning's severity or from the currently selected explorer path.
            "trust": [
                {k: r[k] for k in ("step", "state", "species", "on_route") if k in r}
                for r in (
                    results["trust"] if isinstance(results.get("trust"), list) else []
                )
                if isinstance(r, dict)
            ],
            "route_steps": (
                _strlist(results["route_steps"])
                if isinstance(results.get("route_steps"), list)
                else None
            ),
            "solve_label": _solve_label(results, validation_manifest),
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
            "verdict": verdict,
            "v2_solve": d.get("v2_solve"),
        }
    }
