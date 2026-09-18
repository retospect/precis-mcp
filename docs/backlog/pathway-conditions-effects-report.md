---
status: draft
title: Per-pathway conditions-effects report (H / O / OH / solvation / mixed coverage, at several U vs RHE)
prio: high
---

# Per-pathway conditions-effects report

Request (Reto, 2026-09-16): "a short report of 2-3 pages with results and
findings (effect of solvation molecules, effect of surface hydrogen, effect
of surface oxygen, effect of surface hydroxide, effect of mixed coverage)",
per pathway, under a few U vs RHE conditions, for the NO→NH₃ quest
(qu164903). Nothing on prod produces this today: the quest tick proposes
one candidate, harvests a one-line result entry (`quest/compute.py`
"autocatpath result for [st…]: barrier=… eV"), and rewrites the dossier.
No step reads a finished pathway's `analysis`/`trust`/`warnings`/`kinetics`
views and writes findings, and no view compares candidates as a controlled
series (same slab, one thing varied) or at a chosen U.

## Gap audit — what exists per effect (verified 2026-09-16)

| effect | buildable now | measured now | missing |
|---|---|---|---|
| surface H | yes — `add_atom_site` H on hollow; tick prompt offers "optional co-adsorbate (e.g. H)"; engine scores the given slab (`template: coadsorbed`) | ad hoc: dossier has Pt+1/2/3H subsurface trends; catpath RUNLOG 2026-08-14 hsub1 vs hsub4 | a θ_H series view; `meta.params` stamp so pathways group by "same slab, n_H varied" |
| surface O | yes — `add_atom_site` O | none on prod (25 newest pathways: no O/OH co-adsorbate; H₂O only as the μ_O reservoir) | same as H |
| surface OH | clumsy — O by site + H by raw `frac`; no group placement | none | `add_adsorbate` op (species + site, upright OH geometry) |
| solvation molecules | explicit H₂O spectators are placeable as atoms; engine has NO solvation model (catpath CONFIG §Electrochemistry scope; `beastdb-solvation-shifts` draft blocked on `beastdb-access-audit`; `jdftx-gcdft-backend` idea); CDQ §9 lists solvation "not v1" | none | Reto decision (below) |
| mixed coverage | engine `autocatpath coverage` = γ(θ) ab-initio-thermodynamics scan, **v1 one species per termination, no lateral interactions**; precis never runs it (`precis_pathway/runner.py::run_kinetics` "no coverage scan runs here", `mari` always None) | none | precis `autocatpath_coverage` job + view; engine v2 mixed terminations |
| U vs RHE | engine CHE closed-form per pathway: `U_L`, `U_opt`, `span_at_UL`, `span_at_Uopt`, `P_side` (T=298.15); `conditions.potential_U` config exists; viewer re-renders any U **client-side only** | scalars harvested onto candidates; qu164903 rubric minimises span_at_Uopt / U_L_abs / energy / P_side | no `get(kind='pathway', view=…, args={'U': x})` (handler takes no `args`); no cross-candidate `compare` at a chosen U; barriers are U-independent (CHE is thermodynamic-only — report must say so) |

Selectivity today: P_side null on 23/23 schema-2 pathways (no_barrier from
best-first pruning + `seeds:[0]`). Worktree
**imperative-churning-bumblebee** (gr343666 rung filter, screening-tier
selectivity non-blocking, trusted-barrier neb→verify escalation, ≥3 seeds
at verify) is the prerequisite for any selectivity finding in the report —
wait for it to land; do not touch `quest/compute.py`/`frontier.py` in
parallel with it.

## Plan (phased; 0 first, it is the "properly analyze what we have" step)

### Phase 0 — analysis pass with today's tools (no code)
Dispatch one agent (Sonnet) against prod read-only: take qu164903's
trusted pathways since 2026-09-02 (15 `barrier_trusted`), read
`view='analysis'|'trust'|'warnings'|'kinetics'` per pathway plus the
frontier, group by slab composition × co-adsorbate count from the
structure ops, and write a local report (`~/precis-experiments/
qu164903-effects/report-2026-09-XX.md`) with: H effect (only effect with
data), U readings at U=0 / U_L / U_opt per pathway, and a **shortcomings**
section — which views were insufficient, which numbers untrusted, which
effects have zero data. File the tool gaps as gripes. Output feeds Phases
1–2 with evidence instead of guesses.

**DONE 2026-09-17** (Sonnet, read-only, against the e91c791e deploy):
`~/precis-experiments/qu164903-effects/report-2026-09-17.md`, 25 pathways
/ 15 structures. Findings: no clean H-effect read (the two H-bearing
candidates also differ in dopant count); zero O/OH/H₂O co-adsorbates
anywhere (gap audit confirmed exactly); 11/24 barriers untrusted, nearly
all the `wrong_binder` shape catpath 0.21.0 fixes (data predates it);
pw341034's U_opt (−41.5 V) is a degenerate 0 eV off-route NEB saddle the
CHE scalars never cross-check. Gripes: gr345340 (`label_hi` drops a
`set_element` dopant), gr345341 (U_opt/span not gated by the barrier's
trust checks), gr345342 (results-table `site` is dopant-only). Unverified
lead: the two Ag-subsurface candidates show 1 reconstruction warn each vs
32–74 elsewhere.

### Phase 1 — precis views + ops (small, this repo)
1. **DONE 2026-09-18** — `get(kind='pathway', view='analysis'|'profile'|'compare',
   args={'U': x})`: `precis_pathway/analysis.py::at_potential` ports the
   viewer's `G(U)=G(0)+n_H·eU` shift (+ `most_endergonic_step`); `compare`
   at U ranks by span at U; a pre-CHE graph (no `n_H`) refuses the lever.
   Backlog cross-ref `pathway-profile-renderer-unification`.
2. **DONE 2026-09-18** — `meta.params` writer at proposal time
   (`quest/compute.py::params_from_spec`, stamped by `ensure_candidate`):
   `{dopant, n_dopant, site, coads: {species: n}, coads_site}` derived from
   the ops, proposer-supplied `proposal['params']` overriding. Closes
   gr345342 (the results table's `coads` now carries the named site,
   `H2@hollow`). `get(kind='quest', view='series')` groups candidates that
   differ in exactly one axis (dopant / n_dopant / site / coads) into
   blocks of span_at_Uopt / U_L / P_side / barrier / trusted per level;
   it recognises series, it does not enumerate a grid (decision 4).
3. **DONE 2026-09-18** — `add_adsorbate` structure op: species (H · O · N ·
   OH · H₂O · NH · NH₂ · NH₃) + a named site (the same `add_atom_site`
   `{type, anchors}` resolver, not the engine's `discover_sites`) → the
   whole group placed at its gas-phase internal geometry with intra-group
   bonds, `rotate` about the surface normal. Documented in
   `precis-structure-help` + the tick's op menu; the params writer counts a
   group as its species (`coads: {OH: 1}`). The upright orientation is a
   pre-relaxation starting guess, stated as such in the op docstring.

### Phase 2 — the report step (quest layer)
1. `precis quest report <id>` (+ loop hook every N ticks): reads the
   series tables (1.2), trust/warnings/kinetics per pathway, and the U
   set; the LLM writes a 2–3 page findings document per effect with an
   auto-populated **scope caveats** block (CHE thermodynamic-only, no
   solvation, coverage v1 single-species, untrusted rows excluded). Home:
   a separate `draft` linked `report-of` the quest (the dossier is
   tick-owned and rewritten wholesale — not a stable home). Figures via
   `quest/figures.py` (profile at U, pareto) → closes
   `quest-artifacts-in-dossier`.
2. Prompt nudge only, no enumerator (Reto's standing call: the agent owns
   the chemistry): when `meta.effect_studies` lists effects, the proposer
   is told to run controlled series (vary one thing on the champion slab)
   and the report step names which series are still empty.

### Phase 3 — engine (catpath repo, separate ship path)
1. `autocatpath_coverage` precis job type + `view='coverage'` (γ(θ),
   winning termination, MARI → `run_kinetics(mari=…)`).
2. Engine coverage v2: mixed terminations (H+O, H+OH) — needed before
   "mixed coverage" means anything.
3. Solvation per the decision below.

## Decisions (Reto, 2026-09-16)
1. **Solvation**: APPROVED (a) — explicit 1–3 H₂O spectators on the slab as
   a crude probe, with the caveat block stating no proton shuttling and
   neglected adsorbate entropy; (b) the tabulated ΔG_solv tier stays a
   catpath follow-up once BEAST DB access is audited.
2. **Report home**: APPROVED — a separate `draft` linked `report-of` the
   quest; the dossier is never the home.
3. **U condition set**: "middle, extremes, maybe one more point". Reading
   taken (amend if wrong): the extremes are the two ends of the meaningful
   window, U = 0 V vs RHE (no driving force, the reference) and U = U_L
   (every PCET step downhill — the most negative potential the report
   needs); the middle is the midpoint of those two; the extra point is
   U_opt when it falls inside the window (it is the minimum-span
   potential, so it belongs in the table). Four columns per pathway:
   0 V · mid · U_opt · U_L.
4. **Series authority: AGENT-OWNED (Reto, 2026-09-16)** — "we should
   check the results of this run before going on to the next one; the LLM
   can make a better call on what to do next." No enumerator, no declared
   grid. The loop already waits for a proposal's sims before the next tick
   (`tick.py` "waits for its sims before the next tick"), so the *cadence*
   is right; what is thin is *what the model sees* of the finished run. The
   tick is a single structured LLM call with no live `get`/`search`
   (`tick.py` §"The tick is a single structured LLM call"), so it cannot
   drill into a pathway's `steps`/`warnings`/`trust` on its own. Today it
   gets: the frontier summary (one line per candidate, measures as an
   alphabetical `k=v` run, capped at frontier + 5 beaten + 10
   provisional), a `Tried: … (BEST)` line carrying only the primary
   measure, `limiting_factor`/`worst_problem` per evaluated candidate, and
   a one-line `result` logbook entry. ⇒ **Phase 1 gains a results-table
   context block** (replaces the series view, 1.2): one aligned row per
   evaluated candidate — composition, dopant + site, co-adsorbates
   (n_H/n_O/n_OH/n_H₂O, from `meta.params`), tier, seeds, barrier,
   span_at_Uopt, U_L, P_side, trusted?, rate-limiting step, top warning,
   worst_problem — sorted by lineage (base slab, then what was varied) so
   a series reads as adjacent rows, uncapped within a token budget
   (`narrative_budget.py` pattern), plus the same table as
   `get(kind='quest', view='results')` for humans and the report step.

## Warnings: why few results are unambiguous (prod, pathways since 2026-09-02, n=24)

| signal | count | source | class |
|---|---|---|---|
| single-seed runs | 24/24 | quest `reaction_config` pins `seeds:[0]` | root cause of `low_confidence` 23/24 |
| selectivity available | 0/23 | best-first pruning + 1 seed ⇒ P_side null | fixed by bumblebee (non-blocking selectivity, verify ≥3 seeds) |
| barrier available | 15/23 | `trust_summary.barrier` | 8 blocked: `wrong_binder` 16 records, `detachment` 4, `multistart` 1, `saddle_verified` 1 |
| "orientation, not a re-seat; NEB allowed" | 182 msgs | `grade_binding` marginal/warn (H-tilt note) | NOISE — informational, emitted into the flat `warnings` list |
| "thermo: no table data for <species>" | ~140 msgs (~6 species × 23) | `thermo.mode: table` lacks ZPE for ammonia-network adsorbates | DATA GAP — one curated table fixes all pathways |
| "wrong-site" | 62 msgs | binding pre-flight re-seat on doped slabs | the dominant FATAL — needs a root-cause look at the 16 records (which step/fragment/site) |
| "barrier untrustworthy, off a non-surface path" | 15 | off-route edges | noise for the route verdict (trust_summary already scopes to route) |
| >10 warnings per pathway | 16/24 | flat list mixes marginal + off-route + fatal | presentation, not physics |

Meaning of "meaningful": barrier available AND ≥2 seeds agree within
`ENDPOINT_TOL_EV` AND selectivity available. Today 15/23 · 0/24 · 0/23.

### Fix order (cheapest first, each independently shippable)
1. **Demote noise (precis, small).** The visible `warnings` (MCP views,
   quest context, web) show only `severity: fatal` fails on `route_steps`;
   marginals and off-route records fold into one count each
   ("3 marginal, 5 off-route") with the trust ids available on demand. The
   `>10 warnings` bucket collapses to the real blockers.
2. **Curate `thermo.species` for the ammonia network** (quest
   `reaction_config`, one-time, literature ZPE with `pc` ids per
   [[cite_sources_rule]]): kills ~140 warnings and moves every G(T,p)
   number onto a consistent scale. Alternative at verify tier:
   `thermo.mode: vib` (measured).
3. **Seeds ≥2 at the neb tier** for the quest (cost ≈2× NEB; offset by
   `reagents=[]` trimming where the network allows) — the only way
   `low_confidence` drops below 96%. Verify tier ≥3 seeds is in bumblebee.
4. **`wrong_binder` root cause (engine + precis).** Pull the 16 records'
   evidence (step, fragment, site, doped neighbour?) — hypothesis: the
   endpoint placement picks a site by clean-Pd heuristics that a dopant
   adatom breaks; the engine's `discover_sites` (GCN-aware, landed
   2026-08-14) should seat endpoints instead. Dispatch `root-cause` before
   any patch.
5. **Reconstruction check calibration** (warn-severity, fails ~93%): Reto
   eyeballs two failing geometries; if physical, catpath switches to an
   envelope-relative threshold (already in the bumblebee purpose as
   "calibration data").
6. Selectivity: land bumblebee, then stamp P_side optional on qu164903.

## Operator steps (Reto runs these — prod mutations are never agent-run)

Root causes confirmed 2026-09-16/17 on the 23 schema-2 pathways:
- **wrong_binder (66 fatal, 2 shapes).** (1) "NO@O … binds through N but * designates O": the template's O-down NO isomer relaxes into the N-down basin — correct physics graded as a wrong site. (2) "N+O+H … fragment NH binds through N but * designates H": N and H are placed as separate specs; they bond during relax and `binding_site_ok` takes the LOWEST-PLACED atom of the merged fragment (the H) as the intended binder. Engine fix in catpath `validate.py`/`trust.py` (heavy-atom binder rule + `fragment_merge` marginal + isomer-collapse marginal).
- **thermo "no table data" (7 species × 23).** Missing adsorbate entries: NOH*, HNO*, HNOH*, H2NO*, NH2OH*, N2O* (+ NO2*/NO3* by template) and gas NH2OH. No citable (111)-surface ZPE table exists in the corpus — the Clayborne 2015 paper (pa965, doi 10.1002/anie.201502104) is ingested WITHOUT its SI, which holds the table. Fix now = measure instead of tabulate: `thermo.mode: vib` (finite-difference adsorbate modes, slab frozen; cheap at 3–5 adsorbate atoms). Later = import the SI, then add cited table entries in catpath `thermo.py`.
- **reconstruction (1115 fail / 83 pass, warn-severity).** `DISP_RATIO_MAX = 0.20` × d_nn(Pd)=2.75 Å ⇒ 0.55 Å; observed min 0.55, median 0.92, max 4.46 Å. Baseline is the as-built state on the input slab (`reconstruction_ok(st.build(slab), res.atoms)`).

1. Seeds + measured thermo on the quest (one statement; re-check the JSON path first with `SELECT meta->'reaction_config' FROM refs WHERE kind='quest' AND ref_id=164903;`):
   ```sql
   UPDATE refs SET meta = jsonb_set(jsonb_set(meta,
       '{reaction_config,search,seeds}', '[0, 1]'),
       '{reaction_config,thermo}', '{"mode": "vib"}')
   WHERE kind='quest' AND ref_id=164903;
   ```
   via `scripts/prod-psql`. Effect: next dispatches run 2 seeds (2× NEB cost; multistart 3 already set at the neb tier) and measured ZPE. Existing candidates are not re-run (content key changes only for new dispatches). If vib proves noisy on a partial, revert the thermo key alone.
2. Reconstruction eyeball (decision: envelope-relative threshold or not): open `/refs/structure/202743` (Pd111-Ag-subsurf-1) and its pathways `/refs/pathway/343043` (step HNOH+H→NH2OH, disp_max 0.55 Å, ratio 0.202, no bonds broken) and `/refs/pathway/341805` (O+H→OH, 0.56 Å, 0.204). If those 0.55 Å moves look like ordinary adsorbate-induced relaxation of a doped top layer, the threshold should be measured against the relaxed clean doped slab (campaign baseline), not 0.2·d_nn of the ideal lattice.
3. Import the Clayborne 2015 SI (paper pa965) so the thermo table entries can be cited; then the table tier replaces vib at screening.
4. Land worktree imperative-churning-bumblebee, then stamp P_side optional on qu164903 (that worktree's spec).

## Concurrent sessions checked 2026-09-16
imperative-churning-bumblebee (selectivity/verify escalation — prerequisite,
avoid `quest/compute.py`+`frontier.py` overlap); catpath repo clean at
0.20.0, nobody on coverage/solvation; `quest-164903-reset-runbook`
in-progress (report must use post-reset, schema-2 data only); no other
tree touches pathway/quest.

## Residuals (reviewer, 2026-09-17, pre-ship of the warnings view + results table)

- **Results table N+1 on the tick hot path.** `quest/results_table.py::build_results_rows` does one `structure_load` plus one `links_for` + `fetch_refs_by_ids` per candidate, every tick and every `view=results`. ~150 round trips at 50 candidates — tolerable today (ticks are minutes apart), but wants a batch structure/links fetch before quests grow. Not fixed: needs a store API, a design call.
- **Web vs TOON on a legacy pathway with zero prose warnings.** `warnings_toon` always prints the "pre-trust-schema artifact" note; the web detail page hides the whole block when prose, blocking and counts are all empty. Left as is: on the web page an empty block reads as noise, and the LLM surface (TOON) is the one that must be explicit. No web-side test covers `_pathway_warnings_sections`; add one when the template next changes.

## Fix plan after Phase 0 (2026-09-17 evening; synthesis stopped, fixes first)

Order = cheapest correctness fix first; each its own worktree cycle + /go.

**Status 2026-09-18 (later):** all of **Phase 1 (items 1–3) is DONE** — see
the Phase 1 section above. What remains in this whole item: the catpath-side
half of fix-plan item 2, then Phase 2 (the report step) and Phase 3
(engine). Reto's operator steps are unchanged and still his.

**Status 2026-09-18:** items 1–3 LANDED (309766b4, 6cc41ad2 — qlanded, gate
debt on the next /go). Item 1 was reframed: `label_hi` is `next_label`'s
label high-water mark by design; the missing piece was a live-element
rollup, now `meta.composition`. Item 2's engine-side half is still open:
catpath should not emit `U_opt`/`span_at_Uopt` over a route whose barrier
is blocked (or should emit the blocker ids alongside) — do it in the next
catpath bump. Item 4 (gr345336) is owned by the gripe-fix loop
(root-caused to a third Anthropic quota wording; fix landed as 7da34dae) —
not part of this thread. Item 5 (gr345354) LANDED with this note: the
frontier headline now says which required objective the converged
candidates lack (and which axes are optional) instead of "(none converged
yet)". Note qu164903 already flags P_side optional; its empty frontier is
the trusted-barrier candidates lacking `span_at_Uopt`/`U_L_abs` (pre-CHE
harvests) — re-harvest or a fresh aggregate fills them. Remaining: item 6
via Phase 1, plus the catpath-side half of item 2.

1. **gr345340 `label_hi` after `set_element`** — `store/_structure_ops.py`
   `_label_hi(scene)` is not re-derived by the substitution op, so a doped
   slab summarises as clean. Recompute from atoms after every element-changing
   op; test = set_element on a Pd slab → label_hi carries the dopant. Small.
2. **gr345341 CHE scalars not trust-gated** — `U_L`/`U_opt`/`span_at_Uopt`
   arrive from the engine's aggregate (`precis_pathway/_dispatch_common.py`
   harvest contract) and are stored regardless of the supplying edge's trust.
   Precis-side gate first: null the scalars (+ a `blocked_by` note) when the
   route edge that sets them has a fatal-fail record or a 0 eV saddle; the
   engine-side fix (catpath, don't emit them) follows in the next catpath
   bump. pw341034 (U_opt −41.5 V) is the regression fixture. Medium.
3. **gr345353 unbudgeted `view='results'`/`'frontier'`** —
   `handlers/quest.py` `_render_results` passes no budget; default to the
   tick's 2500-token budget, accept `args={'budget': N}`, print the dropped
   row count. Frontier rendering (`_render_frontier`) gets the same cap.
   Small; mine.
4. **gr345336 tick output "unparseable"** — `utils/llm/json_reply.py`
   `extract_json_object` rejects a reply ending in a bare `}` with no fence;
   root-cause pass first (truncation vs format), then fix + fixture from a
   real failed job transcript. Small–medium.
5. **gr345354 frontier headline "(none converged yet)" with 13 trusted
   barriers** — `quest/frontier.py`; owned by worktree
   imperative-churning-bumblebee until its /go lands; hand over or do after.
6. **gr345342 results-table `site` dopant-only** — CLOSED 2026-09-18 by
   Phase 1 item 2 (`meta.params` writer with co-adsorbate site).

Then Phase 1 items 1–3 as written above. Pending docs-only qland from
worktree jaunty-swinging-pixel (runbook promotion + this note) rides after
bumblebee's deploy.
