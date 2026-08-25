// Kinetics panel for the pathway detail page — vendored from the catpath run
// report (autocatpath/report.py, the `kinetics panel` IIFE in _PAGE_SCRIPT) so
// the web page and the offline report render the same record the same way.
// Keep in sync with the engine's panel; the server-side trim/verdict half of
// the port lives in precis_web/pathway_kinetics.py.
//
// Deviations from the report version, all because this renders a stored ref
// rather than a run folder: the payload arrives in the #kinetics-data JSON
// script tag (not window.CATPATH_REPORT), and the tier table has no `record`
// file-link column (there is no side-car file to link).
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
    "coverage": "Fraction of surface sites occupied by that state (theta). " +
           "The values sum to 1 across the surface, bare sites included.",
    "bracket": "Steps with no computed barrier are excluded rather than " +
           "guessed. The bracket prices them at both limits -- never, and " +
           "as fast as thermodynamics allows -- so you see whether the " +
           "missing barriers could change the answer.",
    "net rate": "Forward minus reverse flux through a step at steady state. " +
           "Negative = that step runs backwards.",
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
           "(desorption minus readsorption), per site per second. " +
           "Negative = net consumption (a reactant).",
    "selectivity": "The target's share of all net gas production, " +
           "counting only gases with a positive net rate."
  };
  const term = (t, label) => TERMS[t]
    ? "<span class='term' title='" + esc2(TERMS[t]) + "'>" +
      esc2(label || t) + "</span>"
    : esc2(label || t);
  let h = "<h2>Kinetics &mdash; " + term("mean-field") + " " +
          term("microkinetics") + " at the stated conditions</h2>";
  // state the conditions the header promises (tiers share the run's)
  const cond = (K.ml && K.ml.conditions) || (K.dft && K.dft.conditions);
  if (cond) {
    const ps = Object.entries(cond.pressures || {});
    h += "<div class='caption'>T = " +
         Number(cond.temperature).toFixed(2).replace(/\.?0+$/, "") +
         " K &middot; " +
         (ps.length
           ? ps.map(kv => "p(" + esc2(kv[0]) + ") = " + sci(kv[1]) +
                          " bar").join(" &middot; ")
           : "all gas species at the 1 bar reference pressure") +
         "</div>";
  }
  // the verdict: the server synthesised it from the numbers below by fixed
  // rules, and says so -- it is a reading aid for the tables, not evidence
  // it describes ONE tier -- the same one the rate-control bars below use --
  // so it has to say which, or a hybrid-DFT run reads as if the headline
  // covered the measured numbers
  const V = (K.ml && K.ml.verdict) || (K.dft && K.dft.verdict);
  const vTier = K.ml ? "ML tier" : "DFT tier";
  if (V) {
    h += "<div class='verdict " + esc2(V.tone || "") + "'><span class='vhead'>" +
         esc2(vTier) + ": " + esc2(V.headline) + "</span>" +
         ((V.lines || []).length
           ? "<ul>" + V.lines.map(x => "<li>" + esc2(x) + "</li>").join("") +
             "</ul>" : "") +
         ((V.caveats || []).length
           ? "<div class='vcav'>Caveats: " +
             esc2(V.caveats.join("; ")) + ".</div>" : "") +
         "<div class='vrule'>Synthesised from the numbers below by fixed " +
         "rules &mdash; thresholds and sentence templates, no model in the " +
         "loop. Read it as a pointer into the tables, not as a result.</div>" +
         "</div>";
  }
  h += "<table><tr><th>tier</th><th>" + term("TOF", "TOF (/site/s)") +
       "</th><th>" + term("5-95 % band", "5–95 % band") + "</th><th>" +
       term("span limit") + "</th><th>" + term("MARI seed") + "</th></tr>";
  [["ml", "ML"], ["dft", "DFT (hybrid: unmeasured steps stay ML)"]]
    .forEach(pair => {
      const d = K[pair[0]];
      if (!d) return;
      const band = d.sensitivity && d.sensitivity.tof;
      h += "<tr><td>" + esc2(pair[1]) + "</td><td>" + sci(d.tof) +
           "</td><td>" +
           (band ? "[" + sci(band.p5) + ", " + sci(band.p95) + "]"
                 : "&mdash;") +
           "</td><td>" + sci(d.tof_span_limit) + "</td><td>" +
           esc2(d.mari_seed || "clean") + "</td></tr>";
    });
  h += "</table>";
  // what leaves the surface, per gas -- the selectivity readout
  [["ml", "ML"], ["dft", "DFT"]].forEach(pair => {
    const d = K[pair[0]];
    const P = d ? Object.entries(d.production || {}) : [];
    if (!P.length) return;
    P.sort((a, b) => b[1] - a[1]);
    h += "<div class='caption'>" + esc2(pair[1]) + " tier " +
         term("net gas production") + ": " +
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
      ? "<div class='caption'>" + esc2(pair[1]) +
        " tier: excluded-step " + term("bracket") + " agrees (fast limit " +
        sci(b.tof_fast) + " /site/s) &mdash; steps with no computed " +
        "barrier are not load-bearing at these conditions.</div>"
      : "<div class='kguard'>&#9888; " + esc2(pair[1]) +
        " tier: steps with NO computed barrier are load-bearing. At " +
        "their optimistic bound (barrier = max(0, &Delta;G), a bound, " +
        "never a measurement) the TOF moves from " + sci(b.tof_slow) +
        " to " + sci(b.tof_fast) + " /site/s. Missing barriers: " +
        esc2(((b.load_bearing || []).length ? b.load_bearing
                                            : (b.bounded_steps || []))
             .join(", ") || "(not individually attributed)") +
        ". TOF, coverages and rate control are unreliable until these " +
        "are computed.</div>";
  });
  const base = K.ml || K.dft;
  const tierName = K.ml ? "ML tier" : "DFT tier";
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
      eqTs.map((t, i) => "<tr><td>" + (i + 1) + "</td><td>" + esc2(t.name) +
        "</td><td>" + esc2(t.kind) + "</td><td>" +
        (t.dG_eV == null ? "&mdash;" : Number(t.dG_eV).toFixed(3)) +
        "</td><td>" +
        (t.barrier_eV == null ? "&mdash;" : Number(t.barrier_eV).toFixed(3)) +
        "</td><td>" + sci(t.k_f) + "</td><td>" + sci(t.k_b) +
        "</td></tr>").join("") + "</table></details>";
    h += e;
  }
  const bars = (obj, pos, neg) => {
    const rows = Object.entries(obj || {})
      .filter(kv => isFinite(kv[1]) && Math.abs(kv[1]) > 0.005)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 6);
    return rows.map(kv => {
      const w = Math.min(Math.abs(kv[1]), 1) * 220;
      return "<div class='kbar'><span class='kname'>" + esc2(kv[0]) +
        "</span><span class='ktrack'><span class='kfill' style='width:" +
        w.toFixed(0) + "px;background:" + (kv[1] >= 0 ? pos : neg) +
        "'></span></span><span class='kval'>" + (kv[1] >= 0 ? "+" : "") +
        Number(kv[1]).toFixed(2) + "</span></div>";
    }).join("") || "<div class='caption'>none above 0.005</div>";
  };
  h += "<h3>Degree of rate control " + term("X_RC") + " (" + tierName +
       ")</h3>" + bars(base.drc, "#2b6cb0", "#a33");
  h += "<h3>Thermodynamic rate control " + term("X_TRC") + " (" + tierName +
       "; negative = poison)</h3>" + bars(base.trc, "#2f855a", "#a33");
  const cov = Object.entries(base.coverages || {});
  if (cov.length) {
    h += "<h3>Steady-state " + term("coverage", "coverages") + " (" +
         tierName + ")</h3><table><tr><th>state</th><th>&theta;</th></tr>" +
         cov.map(kv => "<tr><td>" + esc2(kv[0]) + "</td><td>" +
                 Number(kv[1]).toFixed(3) + "</td></tr>").join("") +
         "</table>" +
         ((base.n_coverages || 0) > cov.length
           ? "<div class='caption'>The " + cov.length + " largest of " +
             base.n_coverages + " states; the rest hold the remainder.</div>"
           : "");
  }
  // the warnings: computed every run, and the reader has no way to calibrate
  // the numbers above without them
  [["ml", "ML"], ["dft", "DFT"]].forEach(pair => {
    const d = K[pair[0]], w = d && d.warnings;
    if (!w || !w.length) return;
    h += "<details class='kx'><summary>" + esc2(pair[1]) + " tier: " +
         w.length + " warning" + (w.length === 1 ? "" : "s") +
         " from the solve</summary><ul class='kwlist'>" +
         w.map(x => "<li>" + esc2(x) + "</li>").join("") + "</ul></details>";
  });
  // per-step detail: X_RC says which steps matter, this is the arithmetic
  // behind it. Collapsed, because the verdict above is the headline.
  const steps = (base.transitions || []).filter(t => t.kind === "step");
  if (steps.length) {
    const rows = steps.slice().sort(
      (a, b) => Math.abs(b.net_rate || 0) - Math.abs(a.net_rate || 0));
    h += "<details class='kx'><summary>Every step: " + rows.length +
         " transitions, by |" + term("net rate") +
         "|</summary><table><tr><th>step</th><th>" + term("dG", "ΔG (eV)") +
         "</th><th>barrier (eV)</th><th>" + term("k_f") + "</th><th>" +
         term("k_b") + "</th><th>" + term("net rate") + "</th></tr>" +
      rows.map(t => "<tr><td>" + esc2(t.name) + "</td><td>" +
        (t.dG_eV == null ? "&mdash;" : Number(t.dG_eV).toFixed(3)) +
        "</td><td>" +
        (t.barrier_eV == null ? "<i>none computed</i>"
                              : Number(t.barrier_eV).toFixed(3)) +
        "</td><td>" + sci(t.k_f) + "</td><td>" + sci(t.k_b) + "</td><td>" +
        sci(t.net_rate) + "</td></tr>").join("") + "</table></details>";
  }
  h += "<details class='kx'><summary>Glossary</summary><table>" +
       Object.keys(TERMS).map(t => "<tr><td><b>" + esc2(t) + "</b></td><td>" +
         esc2(TERMS[t]) + "</td></tr>").join("") + "</table></details>";
  host.innerHTML = h;
})();
