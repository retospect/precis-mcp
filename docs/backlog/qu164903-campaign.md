---
status: draft
prio: high
---

# Qu164903 campaign

Grouped 2026-09-26 from 5 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## qu164903 reset runbook (operator: this session + Reto)

_Grouped 2026-09-26; was `quest-164903-reset-runbook`, status in-progress, prio high._

Decision (Reto, 2026-08-27): halt the campaign, retire all previous
calculations, DELETE the logbook entries ("loopy things from the past"),
wipe/re-seed the dossier so it exemplifies the dialectic form. Substrate
now deployed fleet-wide: digest render + `view='logbook'`, tick handle
emission, STATUS:refuted lifecycle + red UI, `pw` step selectors,
relevance-ranked serving, per-step trust consumer (barrier/selectivity
split), site-symbolic `add_atom_site`, `estimate` kind slices 1–2,
autocatpath 0.18.0 everywhere.

### Ordered steps — deletion NEVER precedes salvage

1. **HALT — DONE 2026-08-27.** `tag(kind='quest', id=164903,
   add=['STATUS:dormant'])` applied; the loop reconciler self-rests the
   tick loop the moment a quest is non-active (`quest/loop.py` docstring).
   Reversible: re-tag `STATUS:active` re-arms.
2. **SALVAGE — COMPLETE 2026-08-27.** Two passes: 58 rulings (dossier tree
   + frontier + logbook→Aug-2), then a full tail drain (ALL 4,767 entries)
   adding 31 items + corrections. Artifacts:
   `~/precis-experiments/qu164903-reset/salvage-rulings-2026-08-27.md` (v1,
   inline-corrected) + `salvage-tail-addendum-2026-08-27.md` (items 59–94).
   Key corrections: rulings 15/32/33 invalidated; real noise floor ~1 eV
   same-crystal (not 0.111 eV); z=0.66 rule inverted (correct ≈0.46–0.47);
   live Ir lead 0.994 eV (addendum 79); umbrella qu202467 will cite this
   quest's findings (mint MUST precede wipe).
3. **MINT — APPROVED (Reto, 2026-08-27), graded policy:**
   (a) **established** = process/trust/geometry tier only (~15: rulings
   45–49, 52, 53, 55, 58 + addendum 62, 67, 73, 89–93) — claims that
   survive every barrier number being wrong;
   (b) **hypothesis** (`hypothesis=True` + `testable_by` = discriminating
   re-measurement under 0.18.0) = big-effect chemistry (~15–20: 13/14,
   1–9 qualitative, 74–78, narrow-44…) — never citable as evidence,
   upgraded per-claim by one trusted confirmation;
   (c) refuted pairs where a dead conjecture earns a do-not-repropose
   ledger entry (15-as-stated, 32, 33-mechanism);
   (d) **Ir lead = fresh campaign candidate #1** (unmeasured Ir-adatom
   barrier first); (e) inside-noise rulings + era-1 map (addendum 94) NOT
   minted — era-1 stays in soft-delete; (f) provenance (operator
   interventions, subsurface-H hint) → dossier settled-history section.
   Serve minted findings to the quest (`serves`). Pre-trust caveat goes IN
   the finding body ("qualitative; re-measure under 0.18.0 before citing
   the number"). ⚠ Gate risk: hypothesis mint historically requires ≥2
   motivators across ≥2 source papers — pilot-mint 2 items first, decide
   fallback (motivate from served papers / gate change) before batch.
4. **RETIRE CALCULATIONS.** Engine-version idem keys already re-key
   (0.18.0 pin bump) so nothing dedups onto stale jobs. Mark superseded:
   old pathway refs meta status (`superseded` mechanism already exists in
   precis_pathway/persist.py) — or simply leave; frontier repopulates from
   new measurements only after the dossier/serves reset. Decide with Reto
   whether old structure refs stay (linked history) or get pruned serves.
5. **DELETE LOGBOOK** (explicit operator override of append-only
   convention): delete qu164903's quest_log chunks (DELETE cascades
   embeddings — never UPDATE). Keep ref_events (the generic audit ledger,
   `view='log'`) — it is cheap and separate.
6. **WIPE + RE-SEED DOSSIER** in dialectic form: striving header; one
   section per LIVE hypothesis (hub statement — support handles w/ one
   why-clause — steelman counterargument — discriminating experiment with
   pre-registered BEP branch from `estimate`); settled-rulings section =
   links to the minted fi handles (one linked sentence each); open
   questions (the 3) as the live sections' seeds. Cite forms now
   available: [fi…] [ql…] [pw…~step] [es…] [st…].
7. **PRUNE + RETYPE SERVES**: drop off-topic serves (medical papers on a
   Pd-catalysis quest); keep ~30 load-bearing papers; minted findings
   served. Relevance-ranked serving handles the rest at tick time.
8. **RE-ARM**: re-tag `STATUS:active`. Recommendation: re-arm AFTER the
   estimate validation gate (slice 3: reproduce campaign knowns) or at
   least verify the first fresh tick's prompt renders the new handle-rich
   sections correctly (one manual tick eyeball).

### Status 2026-08-28 — hold lifted + executed; open: tick-4 review, re-arm

Done: logbook deleted (4,767→0, cascades verified, ref_events kept);
dialectic dossier installed verbatim (seed:
`~/precis-experiments/qu164903-reset/dossier-seed-dialectic.md`); 27
off-topic serves pruned (1,241→1,214; 25 finding serves intact). Mint
done earlier: fi263178 + fi263593–617 (minus 263613), 2 refuted-tagged.
Tick-zero replication DISPATCHED pre-hold: 50 valid jobs jb263259–263823
(5×10 structures, unique idem keys `tick0-<st>-r<n>`), results accrue on
job meta.partial; jb263279 invalid (transcription, replaced) — cancel
pending. GPU-slot token missing on MCP put → infra:child-killed attrition
expected; re-put same idem key staggered.

**HOLD LIFTED (Reto, 2026-08-27 late eve) — all held items executed:**
1. ✓ Ledger wipe: 290 live pinned chunks retired (1 ledger container +
   288 nodes + 2 stale frontier-tree containers, dossier ref 164905) via
   `retire_chunk` cascade; both containers ensure-recreate empty on the
   next tick. Verified 0 live pinned remain.
2. ✓ `meta.rubric_objectives` → `[span_at_Uopt, U_L_abs, energy, P_side]`
   all sense=min (the axes compute.py's trusted pipeline emits; U_L_abs
   per the "rubric minimizes |U_L|" contract). Closes the DATA half of
   gripe 263257 — comment there when verified on a live frontier read.
3. ✓ jb263279 was already terminal (`failed`) — no cancel needed.
4. ✓ Fleet deployed ed08d4a9 (all hosts green, 13m54s).
5. OBSERVED HAND TICKS RUN (2026-08-28 early, reason-only, local CLI
   against prod). Four attempts, three latent bugs found+fixed
   (SHIPPED 8e66df95 + deployed fleet-wide 2026-08-28):
   - Tick 1: died `error_max_budget_usd` — claude_p's $0.10 default can't
     fit a dialectic-dossier rewrite. FIX: tier-aware `_tick_llm_max_usd`
     (frontier $2.50 / big $1.50 / else $0.50, env
     `PRECIS_QUEST_TICK_MAX_USD` overrides all tiers).
   - Tick 2: "succeeded" as a SILENT NO-OP — claude_p's `_JSON_BLOCK_RE`
     matches ≤2-deep braces; site-symbolic proposals nest 5 deep, so
     `res.data` came back as the last shallow fragment and shadowed the
     text fallback. FIX: `raw_decode`-based scanner + `_PAYLOAD_KEYS`
     guard in `tick._payload_from_result`. Every armed tick would have
     no-op'd this way. Also found: the "Tried:" dedup line leaked
     pre-trust ≈numbers (model built a "disappeared leads" theory from
     them). FIX: provisional entries render name-only.
   - Tick 3: escalated to FRONTIER (2 dry ticks → "stalled"), opus died
     at the flat $0.50 → the tier map above.
   - Tick 4 SUCCEEDED (opus frontier review, $1.74): 5 logbook entries,
     dossier rewritten, 6 ledger nodes, 1 proposal (Ir-pair 2/9 ML
     coverage test, correct subsurface set_element, parent-linked,
     motivated by fi263612). Writing quality high: self-downgraded H1's
     unsourceable 0.994 headline, "trust-anchor before breadth" decision,
     caught the degenerate-NEB row, novel systemic finding (poison_margin
     negative on EVERY row; SO2 unscreened — pinned as ledger gap).
     Gates all engaged ([unverified model claim] prefix, [buildable]
     lead, review decision entry). CAVEAT: the rewrite flattened the
     seed's `###`-sectioned dialectic into prose paragraphs — content
     survived, form drifted; consider tightening the dossier-format
     prompt if the skeleton matters. searches_run=0 is by design
     (compute=False gates lit-search).
6. NEXT: Reto reviews tick 4's writing (dossier readback + raw payload
   sent in-session) → optional second observed tick with `--compute`
   (would dispatch the Ir-pair sim) → re-arm decision. Fixes must ship+
   deploy before re-arm (worker ticks run deployed code).
Reto's parallel queue: 24 hypothesis approve/sign payloads at
/claim/fi<id>; redlines on the seed dossier welcome (edits cheap).

### Not-too-early assessment (asked 2026-08-27)

Halt + salvage + wipe: NOT too early — substrate deployed, engine trusted.
Full re-arm: slightly early; missing pieces are (a) estimate slice 3's
validate-against-knowns gate, (b) agentic tick (toolset now exists), and
(c) quest layer still never MINTS findings itself (rulings minted by hand
here; hypothesis-mint-from-tick is future work). Re-arm with the
coordinator tick is acceptable; agentic tick can land during the fresh
campaign.

## qu164903 kinetics cutover — prod rollout checklist

_Grouped 2026-09-26; was `qu164903-kinetics-cutover`, status draft, prio high._

<!-- Ops runbook, not a code-change item: the code (catalyst_seed
RUBRIC_OBJECTIVES, frontier.py per-quest axes/viewport/$/rate, the web
axis picker, the PNG twin, the tick prompt's tradeoff guidance) ships
independently and is dark until this checklist runs. Every prod-mutating
step below is executed by Reto, never an agent (`prod-mutation-needs-
user-permission` in memory) — an agent may PREPARE the exact command but
must hand it to the user rather than run it. -->

### Motivation / why

The frontier's default rubric moved from `{barrier, energy, selectivity_margin,
poison_margin}` to `{log_tof, atom_cost, selectivity_margin, poison_margin}`
(`catalyst_seed.RUBRIC_OBJECTIVES`) — `barrier` demotes to a context scalar
(TOF is computed FROM it; ranking on both is a redundant axis) and the new
kinetics/economics measures (`tof`/`log_tof`/`log_tof_p5`/`log_tof_p95`/
`kinetics_trusted`/`kinetics_note`/`drc_top`, later `atom_cost`/
`atom_cost_dearest`) only exist once autocatpath ships the in-process
microkinetics solve (`precis_pathway.runner.run_kinetics` +
`_dispatch_common._kinetics_scalars`; the kinetics module exists from catpath
0.15.0, but the cutover targets >= 0.17 — the current tree with the guard
bracket + rule-based verdict). The code side of this
(precis) ships independently of catpath's own release and of the prod quest's
own `meta.rubric_objectives` — nothing here mutates automatically. This item
is the ordered checklist to actually cut the running catalyst quest (qu164903)
over once catpath 0.17 exists.

### In scope — ordered checklist

> **Interim channel (2026-08-23): private wheel, no PyPI.** catpath stays
> private for now, and its auto-publish is OFF anyway (a GitHub release does
> NOT reach PyPI; publishing is a manual `workflow_dispatch` of
> `workflow.yml` with a typed version confirm). Until publication, 0.17.0
> reaches the sparks as a locally-built wheel (`uv build` in `~/catpath`)
> via the `roles/autocatpath` wheelhouse (`/opt/precis/wheels` on each
> host — installed over the constraints pin every run, survives redeploys;
> see `deploy/roles/autocatpath/defaults/main.yml`). Steps 1–2 below are
> DEFERRED to publication time; step 3's playbook-44 half runs now with
> `-e autocatpath_wheel=$HOME/catpath/dist/autocatpath-0.17.0-py3-none-any.whl`.

1. **(Deferred until publication) Publish catpath >= 0.17.0 to PyPI** from
   green CI: manual `gh workflow run workflow.yml -R retospect/catpath -f
   confirm=0.17.0` (OIDC trusted publishing; uploads both `autocatpath` and
   the `catpath` alias). CI lint was fixed green at catpath `0092f96`.
2. **(Deferred until publication) Bump the precis pin** in `pyproject.toml`:
   `autocatpath>=0.13.0` → `>=0.17.0` in BOTH the `catalyst` extra (~line
   311) and the `catalyst-gpu` extra's `autocatpath[mace]>=0.13.0` (~line
   321), re-lock, ship. CANNOT ship while the package is private — `uv lock`
   has to resolve the pin from PyPI. After this ships + deploys, DELETE the
   wheelhouse wheels (`/opt/precis/wheels/autocatpath-*.whl` on spark/
   castor/pollux) — a lingering wheel overrides any newer published pin.
3. **Deploy the code**: `scripts/deploy` (cluster venvs + melchior — the
   dispatch/harvest side of s3 lives in precis, so melchior needs it) AND
   `ansible-playbook playbooks/44-autocatpath.yml` from the synced main
   tree's `deploy/` (sparks' worker venvs: reinstalls precis-mcp@main +
   engine; NOT covered by `redeploy-precis.yml` — `catpath-dev-deploy` in
   memory). First wheel run adds
   `-e autocatpath_wheel=$HOME/catpath/dist/autocatpath-0.17.0-py3-none-any.whl`;
   later runs pick the wheelhouse copy up automatically.
4. **Prod write — update qu164903's rubric** (Reto only, per
   `prod-mutation-needs-user-permission`; an agent prepares the exact
   one-off CLI/SQL and hands it over, does not run it):
   `meta.rubric_objectives` → the new four-axis vector
   (`catalyst_seed.RUBRIC_OBJECTIVES` — copy verbatim, do not hand-retype).
   Optionally also update `meta.reaction_config` with kinetics conditions
   (temperature/pressures) if the worked example needs them for
   `run_kinetics` to have something to solve over — check
   `precis_pathway.runner.run_kinetics`'s config contract before deciding
   this is needed; if the conditions are already implied by the existing
   `reaction_config`, skip it.
5. **Re-key + redispatch**: `precis quest reset-compute 164903` then
   `precis quest redispatch 164903` (the CLI's `id` arg is the numeric
   ref id, not the `qu`-prefixed handle; prod one-off CLI, `prod-one-off-
   cli-write` recipe in memory — DSN from the melchior web plist,
   `--database-url`, run remotely). Re-keying is already guaranteed by the
   `_AUTOCATPATH_SUMMARY_REV` bump to `s3` shipped alongside this slice
   (`precis/quest/compute.py`) — a pre-s3 aggregate carries none of the
   kinetics keys, so harvest re-derives every candidate's `tof`/`log_tof`
   from scratch on redispatch; no separate migration needed for that part.
6. **Resume dispatch**: clear qu164903's job type out of
   `precis_suspended_job_types` in `deploy/group_vars/all.yml` (confirm the
   current value first — the loop may already be un-suspended per
   `qu164903-loop-fixes-followthrough` in memory; don't double-clear an
   already-empty value), ship, redeploy.

### Post-batch cleanup (added 2026-08-24, after the live cutover)

- **Wipe-window casualties**: a handful of aggregates raced the engine
  wipe/redeploy windows on 2026-08-23 evening and succeeded WITHOUT usable
  kinetics — notes `engine 0.13.0 lacks kinetics` (ran mid-revert) or no
  kinetics keys at all (ran on pre-s3 precis code). Their idem keys are
  consumed, so they never self-heal. After the current batch drains, count
  them (`kind='job'`, `job_type='autocatpath_aggregate'`, succeeded, note
  LIKE 'engine 0.13%' or missing `kinetics_trusted`) and re-run just those
  candidates. Cheapest correct lever: install the 0.17 wheel into
  melchior's venvs too (fixes the engine-version token dispatch stamps
  into idem keys — melchior's pure-[catalyst] autocatpath is still 0.13),
  then `redispatch` — but ONLY once the batch has drained, because the new
  version token re-keys EVERYTHING and would orphan in-flight work.
- **TOF ≈ 0 candidates read as untrusted, not measured-dead**: barriers
  ~1.7–2 eV give true TOFs (~1e-16 s⁻¹) below the ODE solver's numeric
  floor, so the solver returns ±1e-12 noise and the positivity gate stamps
  `tof non-positive (solver anomaly)`. Honest but means dead-slow designs
  sit in the provisional band instead of being dominated on a tiny
  log_tof. If most of the batch lands there, consider a catpath-side
  floor/verdict ("below solver resolution ⇒ report upper bound") — an
  engine change, not precis.

### Explicitly NOT in scope

- Any catpath engine code — that repo cuts its own release on its own
  schedule; this item only reacts to a release existing.
- Backfilling `atom_cost` historically on already-measured candidates —
  it's a LOCAL, sim-free computation (mass-weighted $/kg off the
  composition already on each candidate's `structure.meta`), so once the
  code + rubric are live it backfills itself on the next harvest pass; no
  separate backfill script is needed.
- Any other quest's rubric — this checklist is qu164903-specific; a new
  catalyst quest minted after this ships gets the new four-axis default
  automatically via `catalyst_seed.RUBRIC_OBJECTIVES`.

### Acceptance criteria

- `qu164903`'s `meta.rubric_objectives` reads `log_tof`/`atom_cost`/
  `selectivity_margin`/`poison_margin` in prod.
- A tick against qu164903 harvests `tof`/`log_tof` (or
  `kinetics_trusted=False` + `kinetics_note`) onto at least one candidate,
  and the frontier hub (`/refs/quest/164903`) plots the new axes by
  default (no `?fx=&fy=` override needed).
- The quest's dispatch is un-suspended and ticking again.
- Fan-out proven (the last unproven piece of the 3-spark cutover): fresh
  `autocatpath_seed` jobs spread across spark/castor/pollux — check
  `target_node` distribution on new rows (`kind='job'`,
  `meta->>'job_type'='autocatpath_seed'`, `retired_at IS NULL`).

### Target + blast radius

`pyproject.toml` extras (2 lines), qu164903's own `meta` (prod write, one
quest), `deploy/group_vars/all.yml` (`precis_suspended_job_types`),
`44-autocatpath.yml`'s target venv. No other quest, no schema/migration.

### Open questions / decisions log

- ~~Whether `reaction_config` needs explicit kinetics conditions~~ —
  RESOLVED (2026-08-23, against catpath 0.17.0's `config.py`): no.
  `ConditionsConfig` defaults to standard conditions (298.15 K; every
  unlisted gas sits at the 1 bar reference) and `KineticsConfig` defaults
  are sane (sticking 1.0, product = the run's `target`), so
  `kinetics.solve` runs on the existing `reaction_config` unchanged. It
  emits a stated warning that the product pressure defaults to the 1 bar
  reference — acceptable: the frontier ranks candidates *comparatively*
  at identical conditions. Skip the optional half of step 4.

## Quest findings → claim hubs → nanopubs (NO→NH3)

_Grouped 2026-09-26; was `quest-claim-mint-no2nh3`, status idea._

Gap named by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): prod holds **zero minted nanopubs**; the
NO→NH3 quest's claims live only as quest-log/dossier chunks. The
taproot→nanopub machinery is built and gated (see
`claim-publication-nanopub-ots.md` for the build residue) but has never
been exercised on real quest output.

The ops item, distinct from the machinery item: take qu164903's grounded
findings — e.g. the calibration claims (clean Pd(111) NO dissociation
~2.36–2.40 eV DFT; H-predosing lowers to ~1.58–1.68 eV, literature-backed)
and the campaign's own trusted measurements (Nb-adatom+subsurface-H barrier
1.093 eV, st257869) — mint claim hubs with proper supporters, run review,
and publish the survivors as signed nanopubs. Deliverable is a worked
end-to-end demonstration; it also upgrades the landscape draft's citations
to living-hub form. Watch for: hearsay gates on literature-derived numbers
read via reviews, and whether campaign-internal measurements belong in
taproot at all or need a distinct provenance class.

## qu164903 presentation feedback (Reto, 2026-09-18)

_Grouped 2026-09-26; was `qu164903-presentation-feedback`, status draft, prio high._

The tracked list. Each item gets a decision line (level / owner / blocked-by)
once discussed; items that become independently shippable split off into
their own backlog files with `blocked-by` back here.

| # | feedback (verbatim intent) | surface | status |
|---|---|---|---|
| 1 | "we want to make a Pourbaix diagram — how? for what level? discuss" | new view (pathway/structure/quest) | DECIDED b+c → `surface-pourbaix-view.md` |
| 2 | Legend for the chemistry pathway: chemistry vs electrochemistry steps — dashed vs fixed lines? | pathway profile viewer | discussing |
| 3 | "note somewhere on the page how transition-state energy was calculated" | pathway detail page | discussing |
| 4 | From the compound page (`/structure/<slug>`) get to the pathway and to the Pourbaix — tabs on the same page? UX discussion | structure detail / pathway detail | discussing |
| 5 | Pareto front: another energy axis; be specific what the axes are — detailed text | quest page pareto | DECIDED: `barrier` stays, per-axis definitions under the plot (in flight) |
| 6 | Clicking a reaction on the pareto front shows that thing's details (pathway, …) — solved by the tabbed landing spot if tabs are done well | quest page pareto → detail | discussing |
| 7 | pH slider on the pareto front — do we need to rerun? justify why this can be dynamic | quest page pareto | discussing |
| 8 | Default axes of the pareto plot (all three) considered more carefully | quest page pareto | DECIDED: x U_L_abs · y log_tof · colour barrier, via rubric reorder (Reto's SQL below) |

### Working notes (verified 2026-09-18 against this tree + /Users/reto/catpath)

**What exists today (anchors).**
- Pathway page CHE lever: `templates/refs/pathway_detail.html.j2` `#pw-u-lever`
  — U slider vs RHE (−1.5…0.5 V), a **pH input already exists** but only feeds
  the `sheFromRhe()` readout (`U_SHE = U_RHE − 0.0592·pH`); the diagram never
  re-renders on pH because `analysis.py::at_potential` is pH-free by
  construction. Engine twins: `autocatpath/electrochem.py::u_rhe_to_she`,
  `decoupled_ph_shift` (non-PCET proton step; unused by any qu164903 pathway).
- Chemical vs PCET drawing: chemical steps = solid hump trace with a barrier,
  PCET supply edges = dashed connectors, no barrier; the legend
  (`buildPathLegend`) lists *paths*, not line styles — nothing explains the
  dash.
- TS provenance: the Methods body chunk (bottom of the page, `## Methods`)
  says "climbing-image NEB (5 images, converged to 0.15 eV/A)", model, seeds,
  thermo tier. It is 1500 lines below the profile.
- Structure page (`templates/structure/detail.html.j2`, `routes/structure.py::
  _quest_context`): 3D viewer + relax runs + a quest-context panel listing
  the candidate's pathways (`meta.candidate_ref` lookup) — 1 click to a
  pathway. Pareto point → `/refs/structure/<id>` (the candidate slab), not
  the pathway.
- Pareto defaults (`quest/frontier.py::plot_axes_for`): first two declared
  rubric objectives → for qu164903 `span_at_Uopt` × `U_L_abs`, colour z =
  third objective `energy`. `_AXIS_LABELS` has short labels only, no
  description text. `energy` = the candidate slab's **relaxed total energy
  (eV)** — an absolute total energy, not comparable across compositions.
- Coverage scan (`autocatpath/coverage.py`): γ_ads(θ) = [G(slab+nA) − G(slab)
  − n·μ_A]/area at (T, p) — single species per termination, no lateral
  interactions, **no U input**; precis never runs it (`runner.py::run_kinetics`
  `mari=None`).

**Item 1 — Pourbaix, levels.**
(a) solution-species N Pourbaix (NO₃⁻ … NH₄⁺/NH₃) from tabulated ΔG_f
(pymatgen `PourbaixDiagram`, Persson 2012 PRB 85 235438) — zero compute, says
nothing about the catalyst; a context panel at most.
(b) **surface Pourbaix of the candidate slab** (Hansen, Rossmeisl, Nørskov
PCCP 2008 10 3722): lowest-G termination among clean / H*(θ) / OH* / O* / NO*
/ N* / NH_x* (+ a few pairs) in (U, pH). Per termination
ΔG(U,pH) = ΔG(0,0) − n_e·eU_SHE − n_H⁺·kT·ln10·pH; for PCET n_e = n_H⁺ so vs
U_RHE it is lines in U only. Data = 10–30 relaxations per slab; the engine's
coverage scan produces exactly the γ(θ) half, the CHE shift is closed-form on
top ⇒ level (b) = effects-report **Phase 3.1** (`autocatpath_coverage` job +
`view='coverage'`) + a renderer. Mixed terminations (H*+NO*) need engine v2.
(c) operating-point overlay: the pathway's U_L / U_opt as vertical lines on
(b) — the coverage-consistency check ("is the slab clean at U_L?"). This is
the level that answers the effects report's coverage question.
Home: the **structure (candidate) page** — a Pourbaix is a property of the
slab, not of one pathway. NO3RR data source for sanity: Liu, Richards, Singh,
Goldsmith ACS Catal 2019 9 7052.

**Item 7 — pH slider: no rerun.** Every qu164903 step is PCET; on the RHE
scale pH cancels exactly (CHE). A pH control can only (i) relabel U to SHE,
(ii) shift the product reference by NH₃/NH₄⁺ speciation (pKa 9.25 — closed
form, last desorption step only), (iii) shift a decoupled proton step
(`decoupled_ph_shift`, none exist). A rerun is needed only for beyond-CHE
physics (field / cation / grand-canonical). Page wording: "All steps are
coupled proton-electron transfers, so free energies on the RHE scale are
pH-invariant by construction (CHE). pH re-expresses U on the SHE scale
(U_SHE = U_RHE − 0.0592·pH at 298.15 K); it does not move the points." The
more useful pareto control is a **U slider**: span at U per candidate is
closed-form client-side if each candidate's node (G, n_H) list ships with the
page; log_tof at U needs a microkinetic re-solve (server-side, no new DFT).

**Items 5/8 — axes.** Drop `energy` as an objective (composition bias:
selects heavier dopant loadings, not better catalysts). Proposal: x =
`U_L_abs` (limiting potential, V vs RHE, min), y = `barrier` (rate-limiting
TS, eV, min; kinetic, U-independent) or `log_tof` (max), z colour = `P_side`
(min) once bumblebee makes it non-null, else `barrier`. Replacement "energy"
worth adding later: dopant formation/segregation energy vs clean slab + bulk
references (stability), needs `e_bulk`-style references. Inconsistency to
state in the axis text: `log_tof`/`span_at_Uopt` are evaluated at each
candidate's own U_opt, `U_L` at U = 0 — candidates are compared at different
potentials. Add `_AXIS_DESCRIPTIONS` (definition, unit, sign, at which U,
reference electrode, T, trust gating) rendered under the plot.

**Items 4/6 — landing spot.** Make the candidate structure page the hub with
URL-addressable tabs (`?tab=structure|pathway|pourbaix|runs`; pattern =
`cad/detail.html.j2` mode buttons): pareto click → `?tab=pathway` showing the
best trusted pathway's profile inline (tier toggle intact) + the list of
others; Pourbaix tab = (b)+(c) when coverage data exists, else a "not
computed — N relaxations" stub. Blocker: the profile viewer is inline JS in
the pathway template — embedding it needs the payload builders factored out
of the pathway route (backlog `pathway-profile-renderer-unification`).

**Items 2/3 — small, shippable now.** (2) legend row: solid = chemical step
(NEB barrier, U-independent) · dashed = PCET (shifts n_H·eU, no barrier in
CHE). (3) one-line provenance footnote under the profile from
`results`/`config`: "TS: CI-NEB, 5 images → 0.15 eV/Å, mace:medium, 1 seed;
barriers carry the reactant's thermo correction; U-independent (CHE)" + the
trust/blocked_by status.

### Open questions / decisions log

- 2026-09-18 Reto: item 1 → levels b+c (surface Pourbaix + U_L/U_opt overlay),
  specced in `surface-pourbaix-view.md`. Item 5/8: `barrier` stays and must be
  defined precisely on the page (it is the largest single-step Ea on the
  route, TS minus that step's own preceding intermediate — NOT the height
  above the slab, which is the span); default y = `log_tof`; drop `energy`.
- Verified definitions (2026-09-18): `barrier` = `precis_pathway/analysis.py::
  rate_limiting_step` (max edge `barrier` on the root→target path, U = 0,
  NEB); `span` = `energetic_span` (Kozuch–Shaik); `log_tof` = microkinetics
  on the U = 0 free energies (`autocatpath/kinetics.py` takes no potential;
  the runner applies no shift) — NOT at U_opt as earlier notes said; `U_L` =
  −max ΔG over PCET steps at U = 0 (`electrochem.py`); `energy` = converged
  relax total energy on the calculator's zero.
- Prod state 2026-09-18 (213 live candidates): `barrier` 205, `U_L_abs` 208,
  `span_at_Uopt` 208, `energy` 196, **`log_tof` 32** (kinetics trust gate),
  `P_side` absent everywhere. A `log_tof` default y plots 15% of points
  today; the picker's "(n)" count makes that visible. Data hygiene: live
  `barrier` max 7462 eV and `span_at_Uopt` max 73.7 eV — pre-0.21 garbage
  rows that should be untrusted, not plotted. Axis block landed 75569104
  (2026-09-19); gripe filed as gr356741 (plausibility clamp in the harvest,
  barrier > 10 eV or span > 20 eV ⇒ trusted=false + reason, plus a prod
  re-harvest). `meta.frontier_viewport` is the stopgap.
- Rubric edit (Reto runs it; defaults follow rubric order via
  `frontier.py::plot_axes_for`): `log_tof` goes in as **optional** so the
  181 kinetics-less candidates stay evaluated on the required axes.
  ```sql
  UPDATE refs SET meta = jsonb_set(meta, '{rubric_objectives}',
    '[{"key":"U_L_abs","sense":"min"},
      {"key":"log_tof","sense":"max","optional":true},
      {"key":"barrier","sense":"min"},
      {"key":"P_side","sense":"min","optional":true}]')
  WHERE kind='quest' AND ref_id=164903;
  ```
  Effect: frontier dominance now includes `log_tof` for the 32 that have it
  and `barrier` for all; `energy` stops steering the proposer. No rerun.
- Items 2, 3 (legend row, TS footnote) and 4/6 (structure hub tabs): still
  Reto's call; 4/6 folds into `surface-pourbaix-view.md` item 3 if approved.

## Surface Pourbaix view (levels b + c)

_Grouped 2026-09-26; was `surface-pourbaix-view`, status draft, prio high, blocked-by qu164903-presentation-feedback._

Decision (Reto, 2026-09-18, presentation feedback item 1): build the
**surface** Pourbaix diagram of a candidate slab (which adsorbate termination
has the lowest free energy at a given potential and pH) and overlay the
candidate pathway's limiting/optimal potentials on it. Not the solution-
species diagram (that says nothing about the catalyst; a context panel at
most, out of scope here). Effects-report Phase 3.1 (`autocatpath_coverage`
job + `view='coverage'`) is the compute half of this item; the two ship
together or 3.1 first.

### Motivation / why
Every qu164903 pathway assumes a termination (clean or one co-adsorbate) and
the CHE lever reads its profile at U_L/U_opt. Nothing checks whether the slab
is actually that termination at that potential — the coverage question the
effects report could not answer. A surface Pourbaix (Hansen, Rossmeisl,
Nørskov, PCCP 2008, 10, 3722) is the standard answer, and the engine already
has the γ(θ) half.

### Physics (what the view computes)
Per termination T (clean, H*(θ), OH*, O*, NO*, N*, NH_x*, later pairs):
ΔG_T(U, pH) = ΔG_T(0, 0) − n_e·e·U_SHE − n_H⁺·kT·ln10·pH. Every PCET
termination has n_e = n_H⁺ = n, so on the RHE scale
ΔG_T(U_RHE) = ΔG_T(0) − n·e·U_RHE — straight lines in U, pH-free. pH enters
only as the SHE relabel (`autocatpath/electrochem.py::u_rhe_to_she`,
−0.0592 V/pH at 298.15 K) and, for non-PCET terminations, via
`decoupled_ph_shift`. ΔG_T(0) per θ = γ_ads·area from the coverage scan
(`autocatpath/coverage.py::scan`: γ_ads(θ) = [G(slab+nA) − G(slab) −
n·μ_A]/area, μ from the gas ledger: μ_H = ½G(H₂), μ_O = G(H₂O) − 2μ_H). The
lower envelope over T at each U is the diagram; the U axis is the pathway
lever's range (−1.5…0.5 V vs RHE).

### In scope
1. **`autocatpath_coverage` job type** (precis side; effects-report Phase
   3.1): `quest/compute.py::dispatch_autocatpath_coverage(store,
   structure_ref_id, config)` mirroring `dispatch_autocatpath` (job tree,
   idem key `autocatpath_coverage:{sha(config, slab_extxyz, model,
   version)}`); executor under `workers/job_types/` wrapping
   `coverage.scan(cfg)`; harvest of the scan's JSON (`facets[].adsorbates
   {frag: {mu, points[{n, theta, energy, gamma, converged}], best}}`,
   `ranking`, `winner`, `mari`). **Blocker to resolve in the engine first**:
   `scan` builds its slab from `cfg.slab.{element, a, miller}` via
   `build_slab()`; it must accept the candidate's own relaxed doped slab
   (the structure ref's geometry), or the diagram is of clean Pd, not the
   candidate. Species list = H, O, OH, N, NO, NH, NH₂, NH₃ at θ ∈ {1/9, 2/9,
   1/3, 2/3, 1} on the 3×3 cell; ~40 relaxations per candidate on the ML
   potential.
2. **Storage**: on the structure ref, `meta.coverage = {model, conditions,
   thermo, area, terminations: [{species, n, theta, dG0, n_e, converged}],
   job_id, version}` (structures have no `view=` mechanism; the page reads
   meta). Store ΔG_T(0) per point, not the envelope — the envelope is
   recomputed client-side at any U/pH.
3. **Renderer** (structure page, new section between "Compute runs" and
   "Quest context", mode buttons per `cad/detail.html.j2`): ΔG vs U_RHE
   lines per termination, lower envelope shaded and labelled by winner
   region; pH field relabels the axis to SHE (same `sheFromRhe` as the
   pathway page); vertical lines at U_L and U_opt of each pathway in
   `_quest_context` (already carries `{ref_id, tier, barrier}`; add `U_L`,
   `U_opt`); a one-line verdict "at U_L = −0.32 V the slab is H*(2/3 ML),
   the pathway assumed clean" when the winner at U_L differs from the
   pathway's `meta.params.coads`.
4. **`get(kind='structure', … )` TOON**: a `coverage` block listing the
   terminations and the winner at U ∈ {0, U_L, U_opt} so the tick can read
   it (the tick has no live get; the results-table row gains a `winner@U_L`
   column).
5. Provenance footer: model, T, pressures, thermo tier, "single-species
   terminations, no lateral interactions (engine v1)".

### Explicitly NOT in scope
- Solution-species Pourbaix (pymatgen): separate, optional context panel.
- Mixed terminations (H*+NO*): engine coverage v2 (effects-report Phase
  3.2); the view must state the v1 limitation.
- Grand-canonical / field / cation effects: beyond CHE.
- Re-running pathways on the winning termination automatically: the
  proposer decides (decision 4 of the effects report, agent-owned series).

### Acceptance criteria
- A candidate structure with `meta.coverage` renders the diagram; without it
  the section shows "not computed — ~N relaxations" and a dispatch button
  (same fidelity dropdown pattern as Relax).
- The winner at U_L is stated in words, and the mismatch verdict appears
  exactly when the winner's species differs from `meta.params.coads`.
- pH field moves the axis labels only; a test asserts ΔG lines are
  unchanged at pH 0 vs 7 on the RHE scale.
- Test fixture: a synthetic `meta.coverage` with three terminations whose
  crossings are known by hand; envelope + winner regions asserted.
- `get(kind='structure')` TOON includes the coverage block; results-table
  row shows `winner@U_L`.

### Target + blast radius
`quest/compute.py` (new dispatch), `workers/job_types/` (new executor),
`precis_pathway/` harvest, `precis_web/routes/structure.py` +
`templates/structure/detail.html.j2`, `handlers/structure` TOON,
`quest/results_table.py`. Engine: `coverage.py::scan` slab input (catpath
bump, separate ship path).

### Open questions / decisions log
- Engine slab input (item 1 blocker): add `cfg.slab.atoms_path` (extxyz) to
  `scan` — the same extxyz the seed jobs already ship. Reto to confirm this
  goes in the next catpath bump.
- Species/θ grid above is a proposal; the proposer may narrow it per
  candidate (agent-owned, decision 4).
