---
status: draft
title: NEB trust-gate diagnostic — one read that shows each barrier gate, its inputs, threshold and verdict, plus a zero-compute pooled "would it pass"
pillar: 3d-design
thread: chemistry
prio: high
---

# NEB trust-gate diagnostic (gr515871)

Bottom line: every input the barrier trust gate uses is already stored on
the `pathway` ref. What is missing is a read that puts them together.
Slice 1 adds `get(kind='pathway', id=<pw>, view='gate')`: one row per gate
with its input, threshold and verdict, replayed from stored records. Slice 2
adds `args={'with': [...]}`: it pools the stored endpoint records of
compatible sibling pathways and reruns the endpoint-agreement check over
them, with no new compute. Neither slice needs a migration. Reto ruled this
a separate tool from gr512126 (`neb-structure-builder.md`), 2026-10-25.

## Motivation / why

qu164903 (Pd catalyst, NO→NH₃) filed gr515871 on 2026-10-25. It could not
tell whether st336035's "newly-consistent" barrier (0.9069 / 0.9068 eV)
would clear the multi-seed endpoint-agreement gate. So it dispatched a
3-seed rerun and waited a tick. gr508948 (shipped bb4216879) stamped
`tol_eV` onto every `endpoint_agreement` record and added `seeds=[...]` to
reruns. It did not add a gate-level read.

Prod read, 2026-10-25, of the pathways linked to st336035:

| pathway | engine | seeds | rate_Ea (eV) | gate | blockers |
|---|---|---|---|---|---|
| pw336193 | 0.20.0 | [0] | 1.019 | pass | 0 |
| pw512178 | 0.37.0 | [0,1] | 0.9070 | fail | 5 |
| pw513347 | 0.39.0 | [0,1] | 0.9069 | fail | 4 |
| pw515950 (rerun) | 0.39.0 | [0,1,2] | 1.004 | fail | 11 |

The answer was already in the store before the rerun:

- Both "consistent" readings came from runs that had already failed the
  gate. In both, seed 1 landed in a higher basin for the same four
  route states: NH2, NH2+OH, NH2+OH+H, NH2OH.
- The two runs reused seeds 0 and 1. Their agreement shows the run
  repeats; it is not independent sampling.
- The single-seed pass on pw336193 means nothing: `endpoint_agreement`
  only runs with ≥2 seeds, so one seed leaves the gate silent and
  `trust_summary.barrier.available` reads true.
- In pw515950, seed 1 sits 317–867 meV above the minimum basin on those
  states (tolerance 50 meV). The 3-seed rerun added NO+H and NH2+H2O to
  the failures.

A seed that keeps landing in the same higher basin across engine versions
points to a reproducible second basin (a placement or chemistry question),
not to noise. Agents cannot see that pattern today. `view='trust'` prints
2,235 flat rows for pw515950. `view='warnings'` lists the 11 blockers with
no threshold, no route context and no cross-run comparison. The candidate
carries only `barrier_blocked_by` ids.

## What is stored vs thrown away

| gate input | stored where | status |
|---|---|---|
| gate verdict + blocking ids | `meta.results.trust_summary.barrier` | stored |
| every trust record (schema 2) | `meta.results.trust` | stored |
| route steps | `meta.results.route_steps` | stored |
| route nodes (scope of state records) | — | not stored; derived exactly from `route_steps` (each is `a->b`) |
| per-state energy + fingerprint per seed | `endpoint_identity` records | stored |
| endpoint tolerance | `endpoint_agreement.evidence.tol_eV` | stored since bb4216879; older runs: engine constant, labelled `assumed` |
| per-seed barrier per step | `saddle_verified.evidence.barrier_eV` (seed-keyed) | stored when a band exists |
| per-step barrier samples | `meta.results.edges[].barrier.values` | stored, but not seed-labelled; display only |
| spread tolerance | `meta.config.search.energy_thresh` | stored |
| absurd-magnitude guard, twin disagreement | candidate structure meta (`barrier_absurd`, `barrier_twin_disagreement`) | stored on the candidate (`meta.candidate_ref`) |
| comparability keys | `autocatpath_version`, `results.models`, `results.network_digest`, `config`, `results.seeds` | stored |

Nothing the gate reads is thrown away, so this item needs no write path.
The one derived input is the route-node set; slice 1's replay checks it
against the stored `blocked_by`.

The gate a harvested barrier passes is assembled from three places:

1. **Engine:** `trust_summary.barrier.available`
   (`autocatpath.pipeline._trust_summary`): no `fatal` `fail` record on a
   route step or route state.
2. **precis:** `quest/compute.py::_flag_absurd_barrier`, |barrier| ≤
   `frontier._BARRIER_ABSURD_EV` (8 eV).
3. **precis:** `_flag_barrier_twin_disagreement`: same `geom_hash_c` twins
   at the same tier must agree within `_TWIN_BARRIER_TOL_EV` (0.5 eV).

`barrier_low_confidence` (std > `energy_thresh`, or fewer than 2 samples)
rides along on the candidate and never gates.

## Where it lives

- **On the `pathway` kind, as a new view `gate`.** The gate's inputs and
  verdict live on the pathway ref; the candidate holds only the verdict.
  `view='trust'` keeps its flat record table, so its readers (tick prompt,
  `precis-quest-help`) keep working. `gate` is the summary over it.
- **Not on the structure kind.** `get(kind='structure')` has no meta view,
  and `barrier_blocked_by` on the candidate is a copy. The candidate side
  gets one pointer instead: the tick's barrier-trust line names
  `get(kind='pathway', id='<pw>', view='gate')`.
- **Not on stored barrier measures.** `measures` rows carry the value, not
  the per-seed basins.
- **The evaluator is core and pure:** `src/precis/utils/trust_gate.py`,
  next to `reaction_graph.py`. Input: autocatpath trust records, a route
  scope and thresholds. Output: gate rows. It imports `autocatpath.trust`
  lazily, only for `endpoint_agreement` (slice 2) and `ENDPOINT_TOL_EV`.
  The pathway kind already requires the `[catalyst]` extra, so the engine
  is importable wherever the view runs. Core placement also lets the
  plugin import it (`tests/test_plugin_import_boundary.py`). The
  `struct_neb_runs.trust` column in `neb-structure-builder.md` uses the
  same record shape, so that item's slice 3 `view='neb'` run read can
  render its trust rows through `gate_rows` instead of a second copy.

## MCP surface

**Slice 1:** `get(kind='pathway', id='<pw>', view='gate')`. Read-only,
zero compute.

```
gate · pw515950 · candidate st336035 · mace:medium · seeds [0,1,2] · engine 0.39.0 · tier neb
verdict: BLOCKED · 11 fatal on-route fails · replay agrees with stored trust_summary
rate-limiting: H2NO+H->NH2OH · Ea 0.847 ± 0.938 eV · by seed: s0 0.000 · s1 1.855 · s2 0.685

{gate	verdict	input	threshold	blocking}
endpoint_agreement	fail	14 route states × 3 seeds; 6 states split; worst NH2+OH+H s1 +866.9 meV	tol 0.05 eV (stamped)	10
detachment	fail	H2NO+H->NH2OH s1 image 6: H 4.69 Å off	any fatal fail	1
neb_convergence	pass	13 route steps × 3 seeds	converged	0
saddle_verified	pass	N pass · M marginal (warn)	ts not at endpoint, curvature < 0	0
multistart	pass	K records, all n_valid ≥ 1	n_valid ≥ 1	0
relax_convergence	…
wrong_binder	…
absurd magnitude (precis)	pass	|1.004| eV	≤ 8.0 eV	0
twin disagreement (precis)	not-run	no geom_hash_c twin at tier neb	≤ 0.5 eV	0
spread (info, never gates)	warn	std 0.938 eV on the rate-limiting step	energy_thresh <config> eV	—
```

The sample uses pw515950's stored blockers; N/M/K are route-scoped counts
the builder fills. Then `## endpoint agreement`: one row per route state and seed, showing
`state · seed · e_eV · Δ meV · fingerprint · verdict`. Failing states come
first, and passing states with one fingerprint fold into a count line.
Rules:

- A check that emitted no records on the route renders `not-run` with a
  reason (`1 seed: agreement needs ≥ 2`, `no band`). It never renders
  `pass`. The pw336193 case reads: `verdict: PASS (vacuous: endpoint
  agreement not run, 1 seed)`.
- The `blocking` column counts record ids in the replayed blocked set.
  If the replay differs from the stored `trust_summary.barrier.blocked_by`,
  the header says `replay DISAGREES` and lists the difference. The stored
  verdict stays authoritative.
- Pre-schema pathways (no `trust_schema` 1–2) get the same refusal text
  `view='trust'` uses. A pathway with no graph gets the existing "not
  computed yet" message.
- When `endpoint_agreement` fails, the footer gives one `next=`: the
  slice-2 pooled call over the candidate's compatible siblings, with real
  handles. Before slice 2 ships, it gives `view='trust'` instead.

**Slice 2:** `get(kind='pathway', id='<pw>', view='gate', args={'with':
['pw512178', 'pw513347']})`, or `args={'with': 'siblings'}` for every
computed pathway with the same `candidate_ref`. It pools each pathway's
`endpoint_identity` records, relabelling seeds as `pw<id>/s<seed>`. It
calls `autocatpath.trust.endpoint_agreement` on the pooled list at the
stored tolerance and scopes the result to this pathway's route. It renders
the same table plus two sections:

- `## basins`: per route state, the distinct fingerprints and which
  `pw/seed` landed in each. A seed that lands in the same non-minimum
  basin across pathways is flagged `reproducible split`.
- `## barriers`: per route step and `pw/seed`, `barrier_eV` from
  `saddle_verified`, with the spread across the pool against
  `energy_thresh`.

Pooling rules:

- The same seed number under the same config in two pathways is a
  `repeat`. It counts once for agreement, and the header says how many
  independent seeds remain.
- Refuse, naming the field, when pathways differ in model,
  `network_digest`, slab config (`config.slab`) or tier. Warn but pool
  when only `autocatpath_version` differs.
- The headline says `hypothetical: pooled over N pathways, nothing
  stored`. The view never stamps meta, mints a job or changes
  `barrier_trusted`.

## Slices

Each slice ships alone. Neither touches the engine, a migration or a job.

1. **(S) Single-pathway gate read.** `precis/utils/trust_gate.py` has
   these functions:
   - `route_scope(results)` derives the route nodes from `route_steps`.
   - `replay_barrier_gate(records, scope)` returns the blocked set by the
     engine's `_trust_summary` rule.
   - `gate_rows(records, scope, thresholds)` returns one row per check:
     verdict, input digest, threshold, blocking count, and `not-run` when
     the check is silent.

   Also in slice 1:
   - `view='gate'` in `precis_pathway/handler.py` plus `gate_toon` in
     `toon_views.py`. The precis guards are read from `meta.candidate_ref`.
   - The tick prompt's trust line (`quest/tick.py`, the paragraph that
     names `view='trust'` for `delta_meV`) names `view='gate'` first.
   - `precis-pathway-help` gets a "Reading the trust gate" section with
     the pw515950 example; `precis-quest-help`'s endpoint-agreement
     paragraph points at it.

   Useful alone: the st336035 question ("would it clear?") is answered
   by one read of pw512178, which is `BLOCKED`, seed 1 split on 4 route
   states, before any rerun.
2. **(S) Pooled dry check.** `args={'with': [...] | 'siblings'}`:
   compatibility refusal, seed relabel and repeat dedupe, pooled
   `endpoint_agreement` via the engine function, and the `## basins` and
   `## barriers` sections. Useful alone: it tells a reproducible second
   basin (the same seed splits the same states in every run) from noise
   (the split moves between runs). That decides whether a quest reruns
   with new seeds or changes the endpoint placement.

## Acceptance criteria

**Slice 1:**
- `tests/test_trust_gate.py` (pure, no DB): with a pw515950-shaped
  fixture (trimmed records, `route_steps`, stored `trust_summary`),
  `replay_barrier_gate` reproduces `blocked_by` exactly. A 1-seed
  fixture renders `endpoint_agreement · not-run · 1 seed` and the
  header `PASS (vacuous …)`. A record without `tol_eV` renders
  `tol 0.05 eV (assumed)` from `autocatpath.trust.ENDPOINT_TOL_EV`. A
  tampered fixture whose stored `blocked_by` omits one id renders
  `replay DISAGREES` and names the id.
- `tests/test_pathway_gate_view.py` (handler, fake store): the view is
  listed in the kind's `views`. An uncomputed pathway gets the existing
  "not computed yet" text. A pre-schema pathway gets the `view='trust'`
  refusal text. The candidate's `barrier_absurd=True` renders `absurd
  magnitude · fail`. The view makes no store writes (the fake store's
  write methods are never called).
- Each H2 section of the rendered pw515950 fixture fits in 4,000 chars,
  and the whole body is under 12,000 chars.
- `tests/test_mcp_raise_next_ratchet.py` and
  `tests/test_plugin_import_boundary.py` stay green.
- Post-deploy: `get(kind='pathway', id='pw515950', view='gate')` on
  prod shows `BLOCKED`, 11 blocking, `replay agrees`. `view='gate'` on
  pw336193 shows the vacuous pass.

**Slice 2:**
- `tests/test_trust_gate_pool.py`:
  - Two fixtures where seed 1 lands in the same higher basin flag
    `reproducible split`. Moving it to a different fingerprint in the
    second fixture drops the flag.
  - Same seed and config in both counts as `repeat`, and the header
    reports independent seeds = 2 for pw512178 + pw513347 (seeds 0, 1
    each).
  - A model or `network_digest` mismatch raises `BadInput` naming the
    field, with `next=` set to the call without that pathway. A version
    mismatch pools with a warning.
  - Pooled records are relabelled `pw<id>/s<seed>`, and
    `endpoint_agreement` sees distinct seeds.
- Post-deploy: `view='gate', args={'with': 'siblings'}` on pw515950
  lists pw512178/pw513347/pw336193 (or refuses pw336193 on tier/version,
  naming why). It shows the NH2-family split under seed 1 in every run.

## Explicitly NOT in scope

- Changing any threshold or gate policy: `ENDPOINT_TOL_EV`, the
  vacuous single-seed pass, the twin tolerance, or `barrier_trusted`
  semantics. The view shows them; see the open questions.
- A tolerance what-if (`args={'tol_eV': …}`). It is easy over stored
  records, but it invites tuning the gate to the data. File it separately
  if Reto wants it.
- Any new compute, rerun or job. `put(kind='structure', mode='rerun',
  args={'seeds': […]})` (gr508948) stays the way to get new seeds.
- The `struct_neb` run read. `neb-structure-builder.md` slice 3 owns
  `view='neb'`; this item only makes `gate_rows` reusable for it.
- Engine changes in `../catpath`, such as persisting `route_nodes` or
  seed-labelling `edges[].barrier.values`.
- Writing the diagnostic onto the candidate or the logbook.

## Target + blast radius

New `src/precis/utils/trust_gate.py`. `src/precis_pathway/handler.py`
(one view name, one branch). `src/precis_pathway/toon_views.py`
(`gate_toon`, which reuses `_trust_context`/`_trust_subject`/`_on_route`).
`src/precis/quest/tick.py` (one prompt line). Skills `precis-pathway-help`
and `precis-quest-help`. Read path only: no migration, no meta write, no
job type. `view='trust'`/`view='warnings'` output is unchanged. Post-deploy
look: the two prod reads in §Acceptance, then qu164903's next tick citing
`view='gate'` instead of dispatching a confirmation rerun.

## Open questions / decisions log

1. **Vacuous single-seed pass.** One seed leaves `endpoint_agreement`
   silent, so `trust_summary` reads available (pw336193). Should harvest
   treat a 1-seed neb-tier barrier as trusted? Recommendation: no change
   here; the view labels it `vacuous`. If Reto wants it gated, file a
   follow-up so precis untrusts a neb-tier barrier with fewer than 2
   independent seeds. That changes quest rankings, so it needs its own
   ruling. (Not a blocker.)
2. **`view='gate'` as a new name, or replace the body of `view='trust'`?**
   Recommendation: a new view. `trust` is cited in the tick prompt and a
   skill as the per-record table, and the flat table stays useful for
   record-id lookups. (Not a blocker.)
3. **Pool across engine versions.** Recommendation: warn and pool.
   pw512178 (0.37.0) and pw513347 (0.39.0) are the case the quest asked
   about. Refusing would hide exactly the cross-run basin pattern the read
   exists to show, and the energy delta per state is printed, so a
   version shift is visible. Refuse only on model, network, slab or tier.
