// Kinetics panel for the pathway detail page — vendored from the catpath run
// report (autocatpath/report.py, the `kinetics panel` IIFE in _PAGE_SCRIPT) so
// the web page and the offline report render the same record the same way.
// Keep in sync with the engine's panel; the server-side trim/verdict half of
// the port lives in precis_web/pathway_kinetics.py.
//
// Deviations from the report version, all because this renders a stored ref
// rather than a run folder: the payload arrives in the #kinetics-data JSON
// script tag (not window.CATPATH_REPORT), and the tier table has no `record`
// file-link column (there is no side-car file to link). Verdict navigation
// uses a separate explorer JSON blob so it cannot alter the ported rules.
//
// The 2026-11-01 kinetics-panel-ux gripes (gr549748/9/57/59/63) landed
// web-side first: unit-ful headers, the net-rate wording, sorted per-class
// coverages, the warning help table, -> rendered as a real arrow, and the
// rate-control rework below are NOT yet in autocatpath/report.py — the
// upstream copy wants the same changes ported to the run report.
(function () {
  const host = document.getElementById("kinetics");
  const data = document.getElementById("kinetics-data");
  if (!host || !data) return;
  let K = null;
  try { K = JSON.parse(data.textContent); } catch (e) { return; }
  if (!K) return;
  // quotes included: several of these land in title='...' attributes, and a
  // definition that contains an apostrophe (MARI seed's "clean") would
  // otherwise close the attribute early and spray the rest into the tag
  const esc2 = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                             .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
                             .replace(/'/g, "&#39;");
  // step names render "->" as a real arrow; the raw name stays the key
  const disp = s => esc2(String(s).replace(/->/g, "→"));
  const sci = v => (v === null || v === undefined || !isFinite(v))
    ? "&mdash;" : Number(v).toExponential(2);
  // every piece of jargon on this panel defines itself on hover, and the
  // same table prints as a glossary at the foot of the panel (hover is no
  // use on a touch screen or on paper)
  const TERMS = {
    "TOF": "Turnover frequency: molecules of the product formed per active " +
           "site per second. Negative means the cycle runs in reverse at " +
           "the stated pressures.",
    "microkinetics": "Every elementary step is integrated with its own rate " +
           "constant, rather than the rate being read off a single barrier.",
    "mean-field": "Coverages are treated as well-mixed averages over the " +
           "surface; no islanding, no site-to-site correlation.",
    "5-95 % band": "Where the TOF falls in 90 % of Monte-Carlo samples " +
           "drawn from the energy uncertainties. Width is machine-learning " +
           "spread, not experimental error.",
    "span limit": "The TOF implied by the energetic span alone, i.e. by the " +
           "single largest effective barrier -- a ceiling for comparison, " +
           "not the solved rate.",
    "span": "Energetic span: the effective barrier of the whole cycle, the " +
           "gap between its rate-determining intermediate and transition " +
           "state (Kozuch-Shaik).",
    "MARI": "Most abundant reaction intermediate: the state holding the " +
           "largest share of the surface.",
    "MARI seed": "The intermediate the solver was seeded from; 'clean' " +
           "means it started from the bare surface.",
    "X_RC": "Degree of rate control (Campbell): how much the TOF responds " +
           "to stabilising one transition state. 1.0 = that step alone sets " +
           "the rate; near 0 = changing it does nothing.",
    "X_TRC": "Thermodynamic degree of rate control: the same sensitivity " +
           "for the stability of a STATE rather than a step. Negative = a " +
           "poison, stabilising it slows the cycle down.",
    "coverage": "Fraction of the site class's sites occupied by that " +
           "state (theta, dimensionless). Each site class (fcc, hcp, ...) " +
           "sums to 1 independently, bare sites (*) included.",
    "bracket": "Steps with no computed barrier are excluded rather than " +
           "guessed. The bracket prices them at both limits -- never, and " +
           "as fast as thermodynamics allows -- so you see whether the " +
           "missing barriers could change the answer.",
    "net rate": "Flux through this step within the full network: " +
           "k_f·θ(from) − k_b·θ(to), evaluated at the steady-state " +
           "coverages. It depends on every other step through those " +
           "coverages; only k_f and k_b are the step's own rate " +
           "constants. Negative = that step runs backwards.",
    "dG": "Reaction free energy of the step at the stated temperature.",
    "k_f": "Forward rate constant, per second.",
    "k_b": "Reverse rate constant, per second.",
    "master equation": "The coupled linear rate equations over every " +
           "surface state: each state's coverage grows by every transition " +
           "into it and shrinks by every transition out, solved to steady " +
           "state (dθ/dt = 0).",
    "detailed balance": "Every reversible pair satisfies k_f/k_b = " +
           "exp(-ΔG/k_BT) exactly, by construction -- the reverse " +
           "constant is never fitted independently of the forward one.",
    "net gas production": "Net rate each gas leaves the surface " +
           "(desorption minus readsorption), per site per second, from " +
           "the chain solve. Negative = net consumption (a reactant).",
    "selectivity": "The target's share of all net gas production, " +
           "counting only gases with a positive net rate."
  };
  const term = (t, label) => TERMS[t]
    ? "<span class='term' title='" + esc2(TERMS[t]) + "'>" +
      esc2(label || t) + "</span>"
    : esc2(label || t);
  // Solve warnings -> plain meaning + what to do (gr549759). One table next
  // to where the warnings are rendered; `re` is matched by new RegExp()
  // against the raw engine line (kept to a JS/Python-shared regex subset —
  // tests/precis_web/test_pathway_kinetics.py pins every known engine
  // message to an entry so a new one is noticed when it renders raw).
  // sev: "bad" the page's numbers can't be trusted as stated · "fix" an
  // assumption the reader can change · "info" a model limit, for
  // information. `subj` names the entity capture (lists use `split`).
  // Global warnings have no entity: their raw lines stay visible inline.
  const WARN_HELP = [
    {re: "mkm: TOF \\S+ is within 10x the balance residual noise", sev: "bad",
     name: "headline TOF unresolved",
     help: "The mean-field TOF sits below the solver's own balance residual, so its sign and magnitude carry no information — read it as no resolved rate, not a small one. No user-side fix on a stored record."},
    {re: "chain TOF \\S+ is inside its cancellation noise", sev: "bad",
     name: "chain TOF unresolved",
     help: "The chain solve's TOF is inside the subtraction's round-off noise — zero within noise, not a rate."},
    {re: "mkm: steady state resolved only to \\|d ln theta\\|", sev: "bad",
     name: "steady state not settled",
     help: "A near-singular balance Jacobian (a slow mode): coverages and TOF carry roughly that relative uncertainty — order-of-magnitude at best."},
    {re: "mean-field MKM failed", sev: "bad",
     name: "mean-field solve failed",
     help: "The site-balance solve did not converge; every v2 number that depends on it (MF TOF, coverages, MF rate control) is absent, so the panel falls back to the chain solve's."},
    {re: "mean-field observable \\S+ (?:failed|skipped)(?: for ([^(]+?) \\()?", sev: "bad", subj: 1, split: true,
     name: "mean-field observable missing",
     help: "That perturbed re-solve failed or hit its time budget; the observable it feeds (orders, apparent Ea, MF DRC/TRC) is not on the record."},
    {re: "thermodynamic DRC is undefined", sev: "bad",
     name: "no thermodynamic rate control",
     help: "The steady-state TOF is not positive, so the sensitivity could not be formed. Follows from the TOF problem; any X_TRC bars shown come from the other solve."},
    {re: "DRC is undefined", sev: "bad",
     name: "no rate control from this solve",
     help: "Same cause as the TRC line: no positive steady-state TOF to perturb about; any X_RC bars shown come from the other solve."},
    {re: "X_(?:RC|TRC) of (.*?) is undefined", sev: "bad", subj: 1,
     name: "a rate-control entry is undefined",
     help: "A perturbed re-solve gave no positive TOF; that entry is absent rather than zero."},
    {re: "sum of X_RC .* differs from 1", sev: "bad",
     name: "X_RC sum rule failed",
     help: "X_RC should sum to ~1 for a converged steady state; the solve may be unconverged or the network mis-specified. Do not read the bars as a clean ranking."},
    {re: "\\d+ of \\d+ (?:correlated|Monte-Carlo) draws", sev: "bad",
     name: "sensitivity band is partial",
     help: "The band is built only from the draws that returned a positive TOF — it is biased toward the draws that worked."},
    {re: "GUARD -- excluded steps are load-bearing.*?missing barriers carrying flux: (.*?) --", sev: "bad", subj: 1, split: true,
     name: "missing barriers change the answer",
     help: "Steps with no computed barrier carry flux at their optimistic bound; TOF, coverages and rate control are provisional until those barriers are computed. Re-run the pathway's barrier compute."},
    {re: "steady-state residual .* is large|mkm: steady-state residual(?: on '(.*?)')?", sev: "bad", subj: 1,
     name: "steady state may not be converged",
     help: "The balance residual is large against the rate scale; coverages and TOF are suspect."},
    {re: "the graph has more than one absorbing component", sev: "bad",
     name: "no unique steady state",
     help: "The reported coverages and TOF are the ODE's end state from one starting coverage, not the unique answer."},
    {re: "states with no usable transition.*?: (.*)$", sev: "bad", subj: 1, split: true,
     name: "part of the network excluded",
     help: "Those states play no role in the solve — missing barriers or energies cut them out. The numbers describe the remaining network."},
    {re: "(?:step|link) (.*?) (?:endpoint missing|has no|by any rated source).*excluded", sev: "bad", subj: 1,
     name: "transitions excluded",
     help: "An endpoint energy or a barrier is missing and the step was dropped rather than guessed; the bracket verdict says whether it could change the answer."},
    {re: "transition (.*?) has \\|dG\\| = .*(?:outside double precision|treated as one-way)", sev: "bad", subj: 1,
     name: "an endpoint energy looks wrong",
     help: "A |dG| this large (over ~15 eV) almost always means a nonsense endpoint energy upstream; no rate through that transition is trustworthy."},
    {re: "no state releases (.*?); the cycle does not close", sev: "bad", subj: 1,
     name: "cycle does not close",
     help: "No desorption channel produces the target, so the steady-state TOF is ~0 by construction — the network or the target is mis-specified."},
    {re: "kinetics_v2 record not written", sev: "bad",
     name: "v2 record incomplete",
     help: "The record itself failed to serialise; whatever is shown is partial."},
    {re: "CONSISTENCY \\S+ failed(?:: edge (.*))?", sev: "bad", subj: 1,
     name: "engine consistency check failed",
     help: "One of the engine's self-checks on this network failed; the numbers carry that check's caveat."},
    {re: "the target committor did not converge", sev: "info",
     name: "BEP flux share approximate",
     help: "The committor solve used to attribute BEP-priced flux did not fully converge; that one number is approximate."},
    {re: "mean-field TOF departs from the chain", sev: "info",
     name: "the two solves disagree",
     help: "Mean-field and chain TOFs differ by more than 0.05 decades — reported, not gated."},
    {re: "no pressure stated for (?:the product )?([^;]+?)(?:;|$)", sev: "fix", subj: 1,
     name: "gas pressures assumed",
     help: "These gases' exchange rates are priced at the 1 bar reference — for a product that over-counts readsorption and distorts selectivity. State partial pressures via put(kind='pathway', mode='recompute_kinetics', args={'environment': {...}})."},
    {re: "sticking uncited: (\\S+)", sev: "fix", subj: 1,
     name: "sticking coefficient assumed",
     help: "The adsorption prefactor uses the default sticking coefficient for these gases; set it in the environment and recompute."},
    {re: "U_vs_RHE is set but electrochemical.provider is null", sev: "fix",
     name: "potential not applied",
     help: "The CHE potential is recorded but shifts nothing until provider: 'che' is set in the environment; recompute under it."},
    {re: "thermo.mode none", sev: "fix",
     name: "pressures reach only prefactors",
     help: "No gas reference carries the kT ln p term, so stated pressures affect only the collision prefactors, not the thermodynamics."},
    {re: "no thermo table data for (.*?)(?:\\(g\\))?(?:;|$)", sev: "fix", subj: 1,
     name: "gas reference is E-based",
     help: "These gases have no thermo-table correction; their references stay raw electronic energies."},
    {re: "could not measure E\\((\\S+)\\(g\\)\\)", sev: "fix", subj: 1,
     name: "cycle closure unpriced",
     help: "This gas's reference energy could not be measured, so its desorption closure is unpriced; supply the reference and recompute."},
    {re: "cannot price the closure (.*?) \\(unpriced gas\\)", sev: "fix", subj: 1,
     name: "a closure desorption was skipped",
     help: "A cycle-closure channel was dropped for lack of a reference energy; if it carried flux the TOF is biased."},
    {re: "MARI (?:guess|fragment) '(.*?)' (?:is not|matches no)", sev: "fix", subj: 1,
     name: "MARI seed not found",
     help: "The seeded most-abundant intermediate is not a network state; the solve started from the clean surface instead."},
    {re: "DFT root '(.*?)' not in the run's nodes|dft.json has no referenced 'rel' energies|DFT tier requested but nothing", sev: "fix", subj: 1,
     name: "DFT tier not applied",
     help: "State energies stay ML-tier; the DFT overlay found nothing applicable."},
    {re: "product '(.*?)' is not a closed-shell gas molecule", sev: "fix", subj: 1,
     name: "target cannot close the cycle",
     help: "The named product cannot be desorbed by the closure rule; re-specify the target."},
    {re: "kinetics.tpd: requested adsorbate.*?: (.*)$", sev: "fix", subj: 1, split: true,
     name: "requested TPD adsorbate not desorbed",
     help: "The network never desorbs these from a bare adsorbate state; check the request."},
    {re: "link (.*?) exchanges more than one gas", sev: "info", subj: 1,
     name: "multi-gas link (model limit)",
     help: "Treated as one barrierless event — no collision-theory prefactor is defined for it (v1 limit). No user action."},
    {re: "link (.*?) supplies an element with no reservoir molecule", sev: "info", subj: 1,
     name: "barrierless link convention",
     help: "An element moves without a reservoir molecule, so the link is treated as barrierless — a network convention, not a measurement."},
    {re: "step (.*?) aggregated .*differs from its endpoints", sev: "info", subj: 1,
     name: "endpoint energy disagreement",
     help: "The step's aggregated energy disagrees with its endpoints' node difference; the solve keeps the endpoint values. Worth checking that step's aggregation."},
    {re: "landing state (.*?) sits .* eV off its base", sev: "info", subj: 1,
     name: "landing state energy flagged",
     help: "Independent relaxations disagree on that state's energy by the stated amount; each transition keeps its own dG. A flag on the state, not on the solve."},
    {re: "no barrier carries a spread", sev: "info",
     name: "no uncertainty distribution",
     help: "No measured barrier spread exists to Monte-Carlo over (single seed and model?); the DRC is the only sensitivity shown."},
    {re: "q_over_k(?: unavailable -- the overall | -- the overall reaction leaves parked fragments \\()(.*?)(?: reaction|\\))|q_over_k", sev: "info", subj: 1, split: true,
     name: "reversibility metric limited",
     help: "The overall-reaction reversibility could not be priced, or parked fragments sit inside its dG."},
    {re: "net has no slab", sev: "info",
     name: "site area assumed",
     help: "site_area_A2 falls back to 6.5 A²; adsorption prefactors carry that assumption."},
    {re: "tpd_corrected offsets applied|electrochemical provider 'che' applied|DFT tier -- .* swapped in", sev: "info",
     name: "engine adjustment applied",
     help: "A stated correction was applied to the energies or rates; shown for provenance."},
    {re: "CONSISTENCY \\S+ did not run", sev: "info",
     name: "consistency check skipped",
     help: "A self-check could not run on this record; nothing failed, it is simply unchecked."}
  ];
  const WARN_RES = WARN_HELP.map(w => new RegExp(w.re));
  const WARN_SEV = {bad: 0, fix: 1, info: 2};
  const warningGroups = ws => {
    // group raw warnings by the help entry they match (same entry -> one
    // line, subjects joined), order groups by severity, leave unrecognised
    // messages verbatim at the end where a new engine message stands out
    const groups = new Map();
    ws.forEach(w => {
      for (let i = 0; i < WARN_RES.length; i++) {
        const m = w.match(WARN_RES[i]);
        if (!m) continue;
        if (!groups.has(i)) groups.set(i, {i, e: WARN_HELP[i], raws: [], subjs: new Map(), fallback: []});
        const g = groups.get(i);
        g.raws.push(w);
        const subject = g.e.subj && m[g.e.subj];
        if (subject) {
          const subjects = g.e.split ? subject.split(/,\s*/) : [subject];
          new Set(subjects.map(s => s.trim()).filter(Boolean)).forEach(s =>
            g.subjs.set(s, (g.subjs.get(s) || 0) + 1));
        } else g.fallback.push(w);
        return;
      }
      const i = WARN_HELP.length;
      if (!groups.has(i)) groups.set(i, {i, e: {sev: "info", name: "unrecognised engine messages, verbatim", help: ""}, raws: [], subjs: new Map(), fallback: []});
      groups.get(i).raws.push(w);
      groups.get(i).fallback.push(w);
    });
    return [...groups.values()].sort(
      (a, b) => WARN_SEV[a.e.sev] - WARN_SEV[b.e.sev]);
  };
  const warningId = (key, i) => "kinetics-warning-" + key + "-" + i;
  const warnList = (ws, d, key) => {
    const row = g =>
      "<li id='" + warningId(key, g.i) + "'><span class='wtag wsev-" + g.e.sev + "' title='" +
      esc2(g.raws.join("\n")) + "'>" + esc2(g.e.name) +
      "</span> " + esc2(g.e.help) + "<ul>" +
      [...g.subjs].map(([s, count]) => "<li>" + warningSubject(s, d, key) +
        " (" + count + ")" + routeLabel(s, d) + "</li>").join("") +
      g.fallback.map(w => "<li>" + disp(w) + "</li>").join("") + "</ul></li>";
    return "<ul class='kwlist'>" + warningGroups(ws).map(row).join("") + "</ul>";
  };
  const solveLabel = (d, key) => d.solve_label ||
    (key === "dft" ? "DFT-refined solve" : "machine-learned potential solve");
  let h = "<h2>Kinetics &mdash; " + term("mean-field") + " " +
          term("microkinetics") + " at the stated conditions</h2>";
  // state the conditions the header promises (tiers share the run's)
  const cond = (K.ml && K.ml.conditions) || (K.dft && K.dft.conditions);
  if (cond) {
    const ps = Object.entries(cond.pressures || {});
    h += "<div class='caption'>" +
         (isFinite(Number(cond.temperature))
           ? "T = " + Number(cond.temperature).toFixed(2)
                         .replace(/\.?0+$/, "") + " K"
           : "T = &mdash;") +
         " &middot; " +
         (ps.length
           ? ps.map(kv => "p(" + esc2(kv[0]) + ") = " + sci(kv[1]) +
                          " bar").join(" &middot; ")
           : "all gas species at the 1 bar reference pressure") +
         "</div>";
  }
  // the panel renders the stored run: it never follows the explorer's
  // U/pH lever (gr549748), which only re-draws the energetics diagram
  if (document.getElementById("pw-u-slider"))
    h += "<div class='caption'>The numbers below are the stored solve at " +
         "these conditions &mdash; they do not follow the U / pH slider." +
         "</div>";
  // the verdict: the server synthesised it from the numbers below by fixed
  // rules, and says so -- it is a reading aid for the tables, not evidence
  // it describes ONE tier -- the same one the rate-control bars below use --
  // so it has to say which, or a hybrid-DFT run reads as if the headline
  // covered the measured numbers
  const V = (K.ml && K.ml.verdict) || (K.dft && K.dft.verdict);
  const base = K.ml || K.dft;
  const tier = K.ml ? "ml" : "dft";
  const vTier = solveLabel(base, tier);
  const baseWarningGroups = warningGroups(base.warnings || []);
  // Navigation is deliberately client-side: the ported verdict and its
  // golden sentence/parity contract remain untouched. Match sentence
  // prefixes, never recompute the scientific thresholds here.
  const verdictSection = text => {
    if (/^(Product-inhibited:|The surface is saturated|No single intermediate)/.test(text))
      return "kinetics-coverages";
    if (/^(Strongest thermodynamic brake:|Thermodynamic rate control|No usable rate control \(X_TRC\))/.test(text))
      return "kinetics-thermodynamic-control";
    if (/^(Rate control|No usable rate control)/.test(text)) return "kinetics-rate-control";
    if (/^(Not determined:|The TOF lies between)/.test(text)) return "kinetics-steps";
    if (/^Not fully selective:/.test(text)) return "kinetics-production-" + tier;
    return "kinetics-tof";
  };
  let explorer = {states: [], paths: []};
  const explorerData = document.getElementById("kinetics-explorer-data");
  if (explorerData) {
    try { explorer = JSON.parse(explorerData.textContent); } catch (e) { /* no links */ }
  }
  const entityHref = (param, selected, value) => {
    const qs = new URLSearchParams(window.location.search);
    qs.set(param, selected);
    if (param === "path") {
      const sameLeaf = explorer.paths.filter(p => p[p.length - 1] === selected);
      // A leaf alone is ambiguous when two routes converge on it.
      if (sameLeaf.length > 1) qs.set("route", JSON.stringify(explorer.paths[value]));
      else qs.delete("route");
    }
    return "?" + qs.toString() + "#pw-explorer";
  };
  const mentions = new Map();
  const addMention = (name, attr, value, param, selected) => {
    if (!name) return;
    mentions.set(name, {attr, value, href: entityHref(param, selected, value)});
  };
  // Only graph states and transitions on an actual explorer path get an
  // entity link. Kinetics-only states keep the sentence's evidence link.
  explorer.states.forEach(s => addMention(s, "data-state", s, "state", s));
  // Same physical-name seam as precis_pathway.flux_share._phys: graph
  // landings can retain ~history that the kinetics solve strips. Prefer
  // an exact state; otherwise select the first matching explorer state.
  const phys = s => String(s).split("~", 1)[0];
  const stateNames = [...(base.coverages || []).map(kv => kv[0]),
    ...Object.keys(base.trc || {}),
    ...baseWarningGroups.flatMap(g => [...g.subjs.keys()].filter(s => !/->|→/.test(s)))];
  stateNames.forEach(name => {
    if (mentions.has(name)) return;
    const state = explorer.states.find(s => phys(s) === phys(name));
    if (state) addMention(name, "data-state", state, "state", state);
  });
  const stepKey = s => String(s).replace(/\s*(?:->|→)\s*/g, "→");
  // Excluded steps and v2-only records may have no rate row. Give a named
  // warning a row with absent numbers, never a made-up rate or barrier.
  const panelTransitions = [...(base.transitions || [])];
  baseWarningGroups.forEach(g => g.subjs.forEach((count, subject) => {
    const pair = stepKey(subject).split("→");
    if (pair.length !== 2 || panelTransitions.some(t => stepKey(t.name) === stepKey(subject))) return;
    panelTransitions.push({name: subject, from: pair[0], to: pair[1], kind: "step", warningOnly: true});
  }));
  const stepIndex = s => panelTransitions.findIndex(t => stepKey(t.name) === stepKey(s));
  const routeLabel = (subject, d) => {
    const records = (d.trust || []).filter(r => [r.step, r.state, r.species]
      .some(s => typeof s === "string" && stepKey(s) === stepKey(subject)));
    const routeSteps = Array.isArray(d.route_steps) ? d.route_steps.map(stepKey) : null;
    const routeStates = new Set((routeSteps || []).flatMap(s => s.split("→")));
    // Same recorded route scope as trust_gate._on_route: a record's step
    // OR state can establish membership. Unknown scope stays unknown here.
    const memberships = records.map(r => typeof r.on_route === "boolean" ? r.on_route :
      (routeSteps && (typeof r.step === "string" || typeof r.state === "string")
        ? routeSteps.includes(stepKey(r.step)) || routeStates.has(r.state) : null));
    // A state can occur on several steps: contradictory/absent evidence
    // cannot license a single on/off-route claim for that subject.
    if (!memberships.length || memberships.some(v => v === null)) return "";
    if (memberships.every(v => v === true)) return " — on-route";
    if (memberships.every(v => v === false)) return " — off-route";
    return "";
  };
  const warningSubject = (subject, d, key) => {
    const i = d === base ? stepIndex(subject) : -1;
    if (i >= 0)
      return "<a class='underline kinetics-step-selection' data-kinetics-step='" + i +
        "' href='#kinetics-step-" + i + "'>" + disp(subject) + "</a>";
    const mention = mentions.get(subject);
    if (mention)
      return "<a class='underline kinetics-selection' href='" + esc2(mention.href) +
        "' " + mention.attr + "='" + esc2(mention.value) + "'>" + disp(subject) + "</a>";
    if (Object.hasOwn(d.production || {}, subject))
      return "<a class='underline' href='#kinetics-production-" + key + "'>" + disp(subject) + "</a>";
    if ((d.coverages || []).some(kv => kv[0] === subject))
      return "<a class='underline' href='#kinetics-coverages'>" + disp(subject) + "</a>";
    // Do not invent a target for a state absent from this stored panel.
    return disp(subject);
  };
  const stepNames = [...Object.keys(base.drc || {}),
    ...((base.bracket || {}).load_bearing || []),
    ...((base.bracket || {}).bounded_steps || [])];
  const linkStep = (name, from, to) => {
    if (!from || !to) return;
    let pi = explorer.paths.findIndex(p => p.some((s, i) =>
      s === from && p[i + 1] === to));
    if (pi < 0) pi = explorer.paths.findIndex(p => p.some((s, i) =>
      i + 1 < p.length && phys(s) === phys(from) && phys(p[i + 1]) === phys(to)));
    if (pi < 0) return;
    const path = explorer.paths[pi];
    addMention(name, "data-kinetics-path", pi, "path", path[path.length - 1]);
  };
  (base.transitions || []).forEach(t => {
    const names = [t.name, ...stepNames.filter(s => stepKey(s) === stepKey(t.name))];
    names.forEach(s => linkStep(s, t.from, t.to));
  });
  // v2-only records can carry sensitivities without the v1 transitions
  // table. Their canonical from->to keys still resolve against the graph.
  stepNames.forEach(name => {
    if (mentions.has(name)) return;
    const pair = stepKey(name).split("→");
    if (pair.length === 2) linkStep(name, pair[0], pair[1]);
  });
  // Longest literal match wins (a whole step before its component states).
  // Sibling anchors avoid invalid nested links; every other text segment
  // still points at the sentence's source section. Escape all text/attrs.
  const names = [...mentions.keys()].sort((a, b) => b.length - a.length);
  const verdictText = (text, section) => {
    const evidence = s => s ? "<a class='underline' href='#" + section + "'>" + disp(s) + "</a>" : "";
    let out = "", start = 0, i = 0;
    while (i < text.length) {
      const name = names.find(n => text.startsWith(n, i)
        && (i === 0 || !/[\w@~+*]/.test(text[i - 1]))
        && (i + n.length === text.length || !/[\w@~+*]/.test(text[i + n.length])));
      if (!name) { i++; continue; }
      const m = mentions.get(name);
      out += evidence(text.slice(start, i)) + "<a class='underline kinetics-selection' href='" +
        esc2(m.href) + "' " + m.attr + "='" + esc2(m.value) + "'>" + disp(name) + "</a>";
      i += name.length;
      start = i;
    }
    return out + evidence(text.slice(start));
  };
  // the v2 solve status, ahead of the verdict: which rung produced the TOF,
  // why the mean-field solve failed, and what the answer is priced against
  const S = (K.ml && K.ml.v2_solve) || (K.dft && K.dft.v2_solve);
  if (S) {
    const bad = S.mf_failure || !S.positive || S.rung === "none" || S.near_noise;
    const FO = S.failed_observables || [];
    // a solve that returned but did not settle, or lost observables, is
    // never the green box: amber, with the reason spelled out below
    const caution = S.unsettled || FO.length > 0;
    h += "<div class='verdict " +
         (bad ? "dead" : caution ? "warn" : "ok") + "'><span class='vhead'>" +
         "v2 solve: " + esc2(S.headline) + "</span><ul>" +
         "<li>TOF rung: " + esc2(S.rung) + "</li>" +
         (S.tof_sign
           ? "<li>tof_mf " + esc2(S.tof_sign.label) +
             (S.tof_sign.detail ? " &mdash; " + esc2(S.tof_sign.detail) : "") +
             "</li>" : "") +
         (S.near_noise
           ? "<li>TOF inside the solver's noise floor: its sign and " +
             "magnitude are unresolved &mdash; read it as &ldquo;no " +
             "resolved rate&rdquo;</li>" : "") +
         (S.unsettled
           ? "<li>state not fully settled: ln-coverage step " +
             esc2(Number(S.state_err).toPrecision(2)) +
             (S.near_noise
               ? " &mdash; no resolved rate"
               : " &mdash; treat TOF as order-of-magnitude") + "</li>" : "") +
         (S.mf_failure && S.mf_stage
           ? "<li>failed at stage: " + esc2(S.mf_stage) + "</li>" : "") +
         (FO.length
           ? "<li>failed observables: " + FO.map(o => esc2(o.name) +
             (o.reason ? " (" + esc2(o.reason) + ")" : "")).join("; ") +
             "</li>" : "") +
         (S.legacy
           ? "<li>no pressures stated &mdash; gases priced at 1 bar " +
             "reference</li>" : "") +
         (S.bep_flux_share != null
           ? "<li>BEP-priced flux share: " +
             esc2(String(S.bep_flux_share)) + "</li>" : "") +
         (S.selectivity_target && !S.selectivity_known
           ? "<li>selectivity to " + esc2(S.selectivity_target) +
             ": unknown (not on the record)</li>" : "") +
         "</ul></div>";
  }
  if (V) {
    h += "<div class='verdict " + esc2(V.tone || "") + "'><span class='vhead'>" +
         esc2(vTier) + ": " + verdictText(V.headline, verdictSection(V.headline)) + "</span>" +
         ((V.lines || []).length
           ? "<ul>" + V.lines.map(x => "<li>" + verdictText(x, verdictSection(x)) + "</li>").join("") +
             "</ul>" : "") +
         ((V.caveats || []).length
           ? "<div class='vcav'>Caveats: " +
             V.caveats.map(x => verdictText(x, "kinetics-warnings-" + tier)).join("; ") + ".</div>" : "") +
         "<div class='vrule'>Synthesised from the numbers below by fixed " +
         "rules &mdash; thresholds and sentence templates, no model in the " +
         "loop. Read it as a pointer into the tables, not as a result.</div>" +
         "</div>";
  }
  h += "<table id='kinetics-tof'><tr><th>tier</th><th>" + term("TOF", "TOF (/site/s)") +
       "</th><th>" + term("5-95 % band", "5–95 % band (/site/s)") + "</th><th>" +
       term("span limit", "span limit (/site/s)") + "</th><th>" +
       term("MARI seed") + "</th></tr>";
  [["ml", "ML"], ["dft", "DFT (hybrid: unmeasured steps stay ML)"]]
    .forEach(pair => {
      const d = K[pair[0]];
      if (!d) return;
      const band = d.sensitivity && d.sensitivity.tof;
      h += "<tr><td>" + esc2(solveLabel(d, pair[0])) + "</td><td>" +
           (d.v2_solve && !d.v2_solve.positive
             ? (d.v2_solve.tof_sign
                 ? sci(d.tof) + " &mdash; " + esc2(d.v2_solve.tof_sign.label)
                 : "no positive steady state (" + sci(d.tof) + ")")
             : sci(d.tof)) +
           "</td><td>" +
           (band ? "[" + sci(band.p5) + ", " + sci(band.p95) + "]"
                 : "&mdash;") +
           "</td><td>" + sci(d.tof_span_limit) + "</td><td>" +
           esc2(d.mari_seed || "clean") + "</td></tr>";
    });
  h += "</table>";
  // what leaves the surface, per gas -- the selectivity readout. The record
  // carries production only from the chain solve: when a v2 rung headlines
  // the TOF the label says so, or the numbers invite a false comparison
  // (gr549748)
  [["ml", "ML"], ["dft", "DFT"]].forEach(pair => {
    const d = K[pair[0]];
    const P = d ? Object.entries(d.production || {}) : [];
    if (!P.length) return;
    P.sort((a, b) => b[1] - a[1]);
    h += "<div class='caption' id='kinetics-production-" + pair[0] + "'>" + esc2(solveLabel(d, pair[0])) + ": " +
         term("net gas production") +
         (d.v2_solve ? " (chain solve)" : "") + ": " +
         P.map(kv => esc2(kv[0]) + " " + sci(kv[1]) + " /site/s")
          .join(" &middot; ") +
         (d.selectivity == null ? ""
           : " &mdash; " + term("selectivity") + " to " +
             esc2(d.product || "the product") + ": " +
             (100 * d.selectivity).toFixed(1) + " %") +
         "</div>";
  });
  // systemic guard: excluded-step bracket (kinetics.py excluded_step_bracket)
  [["ml", "ML"], ["dft", "DFT"]].forEach(pair => {
    const d = K[pair[0]]; const b = d && d.bracket;
    if (!b) return;
    h += b.agree
      ? "<div class='caption'>" + esc2(solveLabel(d, pair[0])) +
        ": excluded-step " + term("bracket") + " agrees (fast limit " +
        sci(b.tof_fast) + " /site/s) &mdash; steps with no computed " +
        "barrier are not load-bearing at these conditions.</div>"
      : "<div class='kguard'>&#9888; " + esc2(solveLabel(d, pair[0])) +
        ": steps with NO computed barrier are load-bearing. At " +
        "their optimistic bound (barrier = max(0, &Delta;G), a bound, " +
        "never a measurement) the TOF moves from " + sci(b.tof_slow) +
        " to " + sci(b.tof_fast) + " /site/s. Missing barriers: " +
        (((b.load_bearing || []).length ? b.load_bearing
                                       : (b.bounded_steps || []))
             .map(disp).join(", ") || "(not individually attributed)") +
        ". TOF, coverages and rate control are unreliable until these " +
        "are computed.</div>";
  });
  const tierName = esc2(solveLabel(base, tier));
  // ---- the equations actually solved -------------------------------------
  // Hand-set HTML math (sub/sup + entities), NOT MathJax/KaTeX — static
  // markup renders identically on paper and in a diff.
  const eqTs = (base.transitions || []).filter(t => t.from && t.to);
  if (eqTs.length) {
    const KBT = "<i>k</i><sub>B</sub><i>T</i>";
    const DG = "&Delta;<i>G</i>";
    const DGX = "&Delta;<i>G</i><sup>&Dagger;</sup><sub>eff</sub>";
    const KF = "<i>k</i><sub>f</sub>", KB = "<i>k</i><sub>b</sub>";
    const eq = s => "<div class='eq'>" + s + "</div>";
    const kSym = (i, back) => "<i>k</i><sub>" + (back ? "&minus;" : "") +
                              (i + 1) + "</sub>";
    const thSym = s => "&theta;(" + esc2(s) + ")";
    // states in order of first appearance; terms of each state's ODE
    const stIx = new Map();
    const terms = new Map();
    const add = (s, sign, t) => terms.get(s).push([sign, t]);
    eqTs.forEach(t => [t.from, t.to].forEach(s => {
      if (!stIx.has(s)) { stIx.set(s, stIx.size); terms.set(s, []); }
    }));
    eqTs.forEach((t, i) => {
      add(t.from, -1, kSym(i, 0) + "&middot;" + thSym(t.from));
      add(t.from, +1, kSym(i, 1) + "&middot;" + thSym(t.to));
      add(t.to, +1, kSym(i, 0) + "&middot;" + thSym(t.from));
      add(t.to, -1, kSym(i, 1) + "&middot;" + thSym(t.to));
    });
    let e = "<details class='kx keq'><summary>The kinetic equations solved" +
      " (" + tierName + ": " + eqTs.length + " reversible transitions " +
      "over " + stIx.size + " states)</summary>" +
      "<h4>The model: a mean-field " + term("master equation") + "</h4>" +
      eq("d&theta;<sub>i</sub>/d<i>t</i> = &Sigma;<sub>j</sub> [ " +
         "<i>k</i><sub>j&rarr;i</sub>&middot;&theta;<sub>j</sub> &minus; " +
         "<i>k</i><sub>i&rarr;j</sub>&middot;&theta;<sub>i</sub> ]" +
         ", &emsp; &Sigma;<sub>i</sub> &theta;<sub>i</sub> = 1" +
         ", &emsp; solved to steady state d&theta;/d<i>t</i> = 0") +
      "<h4>Rate constants, by transition kind</h4>" +
      eq("surface step (computed NEB barrier <i>E</i><sub>a</sub>; " +
         "Eyring): &emsp; " + KF + " = (" + KBT + "/<i>h</i>)&middot;" +
         "exp(&minus;" + DGX + "/" + KBT + "), &emsp; " + DGX +
         " = max(<i>E</i><sub>a</sub>, " + DG + ", 0), &emsp; " + KB +
         " = (" + KBT + "/<i>h</i>)&middot;exp(&minus;(" + DGX +
         " &minus; " + DG + ")/" + KBT + ")") +
      eq("gas exchange (Hertz&ndash;Knudsen in the adsorption " +
         "direction): &emsp; <i>k</i><sub>ads</sub> = <i>s</i><sub>0</sub>" +
         "&middot;<i>A</i><sub>site</sub>&middot;<i>p</i> / &radic;(2&pi;" +
         "<i>m</i>" + KBT + "), &emsp; the reverse never fitted: " +
         "<i>k</i><sub>des</sub> = <i>k</i><sub>ads</sub>/" +
         "<i>K</i><sub>eq</sub>, &emsp; <i>K</i><sub>eq</sub> = " +
         "exp(&minus;" + DG + "/" + KBT + ")") +
      eq("barrierless surface link (network convention): &emsp; " + KF +
         " = (" + KBT + "/<i>h</i>)&middot;exp(&minus;max(" + DG + ", 0)/" +
         KBT + ")") +
      eq("every pair obeys " + term("detailed balance") + ": &emsp; " + KF +
         "/" + KB + " = exp(&minus;" + DG + "/" + KBT + ")") +
      "<h4>Read off the steady state</h4>" +
      eq(term("TOF") + "(" + esc2(base.product || "P") + ") = " +
         "&Sigma;<sub>desorbing " + esc2(base.product || "P") +
         "</sub> ( " + KF + "&middot;&theta;<sub>from</sub> &minus; " + KB +
         "&middot;&theta;<sub>to</sub> )") +
      eq(term("X_RC") + "<sub>,i</sub> = &part; ln TOF / &part; ln " +
         "<i>k</i><sub>i</sub> at fixed <i>K</i><sub>i</sub>, &emsp; " +
         term("X_TRC") + "<sub>,n</sub> = &part; ln TOF / " +
         "&part;(&minus;<i>G</i><sub>n</sub>/" + KBT +
         ") at fixed transition-state energies") +
      "<h4>This run's system, written out</h4>";
    stIx.forEach((_, s) => {
      e += eq("d" + thSym(s) + "/d<i>t</i> = " +
        terms.get(s).map(([sg, t], j) =>
          (sg < 0 ? (j ? " &minus; " : "&minus;") : (j ? " + " : "")) + t
        ).join(""));
    });
    e += "<h4>The numbered rate constants (at the stated conditions; " +
      "pressures already folded in)</h4>" +
      "<table><tr><th>i</th><th>transition</th><th>kind</th><th>" +
      term("dG", "ΔG (eV)") + "</th><th>barrier (eV)</th>" +
      "<th><i>k</i><sub>i</sub> (/s)</th>" +
      "<th><i>k</i><sub>&minus;i</sub> (/s)</th></tr>" +
      eqTs.map((t, i) => "<tr><td>" + (i + 1) + "</td><td>" + disp(t.name) +
        "</td><td>" + esc2(t.kind) + "</td><td>" +
        (t.dG_eV == null ? "&mdash;" : Number(t.dG_eV).toFixed(3)) +
        "</td><td>" +
        (t.barrier_eV == null ? "&mdash;" : Number(t.barrier_eV).toFixed(3)) +
        "</td><td>" + sci(t.k_f) + "</td><td>" + sci(t.k_b) +
        "</td></tr>").join("") + "</table></details>";
    h += e;
  }
  // rate-control bars (gr549763): a colour legend, the value ON the fill
  // itself (the fill carries the number, floored at the number's width so
  // even a tiny |X| shows its figure on the colour), bars scaled to the
  // section's largest |X| so magnitude survives clipping, and an unusable
  // banner the moment a value leaves the physical range -- |X_RC| above ~2
  // is a broken linearisation, not a strong effect
  const srcName = s => s === "mf" ? "mean-field solve"
                             : s === "chain" ? "chain solve" : null;
  const bars = (obj, pos, neg, what, src) => {
    const all = Object.entries(obj || {})
      .filter(kv => isFinite(kv[1]) && Math.abs(kv[1]) > 0.005)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
    if (!all.length) return "<div class='caption'>none above 0.005</div>";
    const maxAbs = Math.abs(all[0][1]);
    // |X| <= 1 keeps the absolute 220 px scale; beyond that the bars are
    // relative to the section's largest value and the caption says so
    const scale = 220 / Math.max(maxAbs, 1);
    let out = "";
    if (maxAbs > 2)
      out += "<div class='kguard'>&#9888; unusual " + esc2(what) +
        " values (largest |value| = " + maxAbs.toFixed(2) +
        "): " + (what === "X_RC"
          ? "X_RC is a normalised sensitivity that usually sums to ~1 " +
            "-- values this far out can signal inhibition or an " +
            "ill-conditioned solve rather than strong control"
          : "values this large are rare for a well-behaved solve") +
        " &mdash; read with care.</div>";
    out += all.slice(0, 6).map(kv => {
      const w = Math.abs(kv[1]) * scale;
      return "<div class='kbar'><span class='kname'>" + disp(kv[0]) +
        "</span><span class='kfill' style='min-width:" + w.toFixed(0) +
        "px;background:" + (kv[1] >= 0 ? pos : neg) +
        "'><span class='kval'>" + (kv[1] >= 0 ? "+" : "") +
        Number(kv[1]).toFixed(2) + "</span></span></div>";
    }).join("");
    out += "<div class='caption'>bar length &prop; |" + esc2(what) + "|" +
      (maxAbs > 1 ? ", scaled to the largest shown" : "") +
      ", floored at the number's width" +
      (all.length > 6 ? " &middot; top 6 of " + all.length : "") +
      (src ? " &middot; " + esc2(src) : "") + "</div>";
    return out;
  };
  const legend = (pairs, src) =>
    "<div class='caption'>" + pairs.map(p =>
      "<span class='ksw' style='background:" + p[0] + "'></span> " +
      esc2(p[1])).join(" &middot; ") +
    (src ? " &middot; " + esc2(src) : "") + "</div>";
  h += "<h3 id='kinetics-rate-control'>Degree of rate control " + term("X_RC") + " (" + tierName +
       ")</h3>" +
       "<div class='caption'>" + esc2(base.drc_note ||
         "No usable rate control.") + "</div>";
  if (base.drc_usable) h += legend([["#2b6cb0", "positive: lowering this step's barrier speeds the cycle"],
               ["#a33", "negative: making the step faster slows the cycle"]],
              srcName(base.drc_source_recorded)) +
       bars(base.drc, "#2b6cb0", "#a33", "X_RC",
            srcName(base.drc_source_recorded));
  h += "<h3 id='kinetics-thermodynamic-control'>Thermodynamic rate control " + term("X_TRC") + " (" + tierName +
       "; negative = poison)</h3>" +
       "<div class='caption'>" + esc2(base.trc_note ||
         "No usable rate control (X_TRC).") + "</div>";
  if (base.trc_usable) h += legend([["#2f855a", "positive: binding this state harder speeds the cycle"],
               ["#a33", "negative: a poison — stabilising it slows the cycle"]],
              srcName(base.trc_source_recorded)) +
       bars(base.trc, "#2f855a", "#a33", "X_TRC",
            srcName(base.trc_source_recorded));
  // coverages (gr549757): largest first, every state above the display
  // threshold shown, hidden states counted. The v2 record's table is per
  // site class -- each class sums to ~1 on its own, so multi-class records
  // are grouped and each class's shown sum stated
  const COV_MIN = 0.001;
  const covBlock = (states, label) => {
    const shown = states.filter(kv => Number(kv[1]) > COV_MIN);
    const hidden = states.length - shown.length;
    const sum = shown.reduce((s, kv) => s + Number(kv[1]), 0);
    const cap = "<div class='caption'>the " + shown.length +
      " shown states hold &theta; = " + sum.toFixed(3) + " of " +
      (label ? "this site class's" : "the surface's") + " sites" +
      (hidden ? "; " + hidden + " state" + (hidden === 1 ? "" : "s") +
        " below 0.001 not shown" : "") + ".</div>";
    return (label ? "<h4>" + esc2(label) + "</h4>" : "") +
      (shown.length
        ? "<table><tr><th>state</th><th>" +
          term("coverage", "&theta; (fraction of sites)") + "</th></tr>" +
          shown.map(kv => "<tr><td>" + esc2(kv[0]) + "</td><td>" +
            Number(kv[1]).toFixed(3) + "</td></tr>").join("") + "</table>"
        : "") + cap;
  };
  const covCls = base.coverage_classes || [];
  if (covCls.length > 1) {
    h += "<h3 id='kinetics-coverages'>Steady-state " + term("coverage", "coverages") + " (" +
         tierName + ", per site class; mean-field solve)</h3>" +
         covCls.map(c => covBlock(c.states || [], c.cls + " sites")).join("");
  } else {
    const states = covCls.length ? (covCls[0].states || [])
                                 : (base.coverages || []);
    if (states.length)
      h += "<h3 id='kinetics-coverages'>Steady-state " + term("coverage", "coverages") + " (" +
           tierName + (covCls.length ? ", mean-field solve" : "") +
           ")</h3>" + covBlock(states, null);
  }
  // the warnings: computed every run, and the reader has no way to calibrate
  // the numbers above without them -- each raw engine line carries its
  // meaning and what to do from WARN_HELP; unrecognised ones stay verbatim
  [["ml", "ML"], ["dft", "DFT"]].forEach(pair => {
    const d = K[pair[0]], w = d && d.warnings;
    if (!d) return;
    // Even a multiplicity caveat without an engine warning has a target.
    h += "<details class='kx' id='kinetics-warnings-" + pair[0] + "'><summary>" + esc2(solveLabel(d, pair[0])) + ": " +
         (w || []).length + " warning" + ((w || []).length === 1 ? "" : "s") +
         " from the solve, grouped by meaning</summary>" + warnList(w || [], d, pair[0]) +
         ((d.verdict && d.verdict.caveats || []).length
           ? "<div class='caption'>" + disp(d.verdict.caveats.join("; ")) + ".</div>" : "") +
         "</details>";
  });
  // per-step detail: X_RC says which steps matter, this is the arithmetic
  // behind it. Collapsed, because the verdict above is the headline. The
  // net rates are the chain solve's -- the v2 record carries no per-step
  // rates, so under a v2 headline the label keeps the two solves apart
  // (gr549748)
  const steps = panelTransitions.filter(t => t.kind === "step" ||
    baseWarningGroups.some(g => [...g.subjs.keys()].some(s => stepKey(s) === stepKey(t.name))));
  const warningChips = t => baseWarningGroups
    .filter(g => [...g.subjs.keys()].some(s => stepKey(s) === stepKey(t.name)))
    .map(g => " <a class='kinetics-warning-chip' href='#" + warningId(tier, g.i) +
      "' title='" + esc2(g.e.name) + "'>warning (" +
      [...g.subjs].filter(([s]) => stepKey(s) === stepKey(t.name))
        .reduce((n, [, count]) => n + count, 0) + ")</a>").join("");
  if (steps.length) {
    const rows = steps.slice().sort(
      (a, b) => Math.abs(b.net_rate || 0) - Math.abs(a.net_rate || 0));
    h += "<details class='kx' id='kinetics-steps'><summary>Every step: " + rows.length +
         " steps, by |" + term("net rate") + "|" +
         (base.v2_solve ? " &mdash; rates from the chain solve" : "") +
         "</summary><table><tr><th>step</th><th>" + term("dG", "ΔG (eV)") +
         "</th><th>barrier (eV)</th><th>" + term("k_f", "k_f (/s)") +
         "</th><th>" + term("k_b", "k_b (/s)") + "</th><th>" +
         term("net rate", "net rate (/site/s)") + "</th></tr>" +
      rows.map(t => "<tr id='kinetics-step-" + panelTransitions.indexOf(t) + "'><td>" +
        disp(t.name) + (t.warningOnly ? " — rates not recorded" : "") + warningChips(t) + "</td><td>" +
        (t.dG_eV == null ? "&mdash;" : Number(t.dG_eV).toFixed(3)) +
        "</td><td>" +
        (t.barrier_eV == null ? (t.warningOnly || t.kind !== "step" ? "&mdash;" : "<i>none computed</i>")
                              : Number(t.barrier_eV).toFixed(3)) +
        "</td><td>" + sci(t.k_f) + "</td><td>" + sci(t.k_b) + "</td><td>" +
        sci(t.net_rate) + "</td></tr>").join("") + "</table></details>";
  } else {
    h += "<div class='caption' id='kinetics-steps'>No per-step transitions recorded.</div>";
  }
  h += "<details class='kx'><summary>Glossary</summary><table>" +
       Object.keys(TERMS).map(t => "<tr><td><b>" + esc2(t) + "</b></td><td>" +
         esc2(TERMS[t]) + "</td></tr>").join("") + "</table></details>";
  host.innerHTML = h;
  // Explorer controls rewrite the address bar after this panel renders.
  // Refresh before native navigation, including new tabs and context menus.
  const refreshEntityHref = ev => {
    const a = ev.target.closest("a.kinetics-selection");
    if (!a) return;
    const state = a.getAttribute("data-state");
    if (explorer.states.includes(state)) {
      a.setAttribute("href", entityHref("state", state));
      return;
    }
    const rawPath = a.getAttribute("data-kinetics-path");
    const pi = rawPath === null ? null : Number(rawPath);
    const path = Number.isInteger(pi) && explorer.paths[pi];
    if (path && path.length)
      a.setAttribute("href", entityHref("path", path[path.length - 1], pi));
  };
  ["click", "auxclick", "contextmenu"].forEach(type =>
    host.addEventListener(type, refreshEntityHref));
  // Fragment navigation must expose collapsed evidence, including on a
  // pasted URL (the panel itself is rendered after the initial HTML).
  const highlightStep = index => {
    const selected = panelTransitions[index];
    if (!selected) return;
    panelTransitions.forEach((t, i) => {
      const row = document.getElementById("kinetics-step-" + i);
      if (row) row.classList.toggle("kinetics-highlight", i === index);
    });
    const segments = [...document.querySelectorAll("#pw-diagram [data-from][data-to]")];
    const matches = (el, physical) => {
      const key = physical ? phys : String;
      // A supply + reaction draws from the pre-supply state. Match the
      // reaction's own source (the folded shoulder), retained by the SVG.
      const from = el.getAttribute("data-reaction-from") ?? el.getAttribute("data-from");
      return key(from) === key(selected.from) &&
        key(el.getAttribute("data-to")) === key(selected.to);
    };
    const exact = segments.some(el => matches(el, false));
    segments.forEach(el => el.classList.toggle("kinetics-highlight", matches(el, !exact)));
  };
  const revealSection = hash => {
    if (!/^#kinetics-[a-z0-9-]+$/.test(hash)) return;
    const target = document.getElementById(hash.slice(1));
    if (!target) return;
    for (let el = target; el; el = el.parentElement) {
      if (el.tagName === "DETAILS") el.open = true;
    }
    const step = /^#kinetics-step-(\d+)$/.exec(hash);
    if (step) highlightStep(Number(step[1]));
    target.scrollIntoView();
  };
  host.addEventListener("click", ev => {
    const a = ev.target.closest("a[href^='#kinetics-']");
    if (a && !(ev.button > 0) && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey && !ev.altKey)
      revealSection(a.getAttribute("href"));
  });
  window.addEventListener("hashchange", () => revealSection(window.location.hash));
  revealSection(window.location.hash);
})();
