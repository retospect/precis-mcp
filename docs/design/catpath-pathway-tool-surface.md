# The `pathway` tool surface — what the LLM sees (TOON-first)

> Plan / proposal. The principle: the LLM **reads and argues with the reaction
> network as data**, not a picture. Everything decision-relevant is a compact
> TOON table (via `precis.format.toon.dump` — same format as `search`), so the
> planner coroutine optimises over *numbers*, not pixels. Mermaid/diagrams are
> deferred (nice for a human reader, not what the loop consumes).
>
> TOON recap: header line is `{col⇥col⇥col}`, rows are `val⇥val⇥val`
> (⇥ = literal TAB). Shown below TAB-aligned for readability.

## The call surface

| Call | When | Returns |
|---|---|---|
| `put(kind='pathway', id, text=<yaml>, mode='preview')` | frame a run — build the network cheaply, **no compute** | the intermediates + steps TOON (argue before spending) |
| `put(kind='pathway', id, text=<yaml>)` | run it (in-process EMT, or routed to the GPU node) | dispatch/summary + the `analysis` TOON |
| `get(kind='pathway', id, view='analysis')` | **the objective** — rate-limiting step, selectivity, verdict | the decision TOON (what the optimiser reads) |
| `get(kind='pathway', id, view='intermediates')` | the states + energies | TOON table |
| `get(kind='pathway', id, view='steps')` | the elementary steps + barriers | TOON table |
| `get(kind='pathway', id, view='profile')` | energy ladder along the path | text ladder |
| `get(kind='pathway', id, view='warnings')` | convergence / geometry issues | TOON table |
| `get(kind='pathway', id, view='config' \| 'methods')` | the config snapshot / citable methods paragraph | text |
| `search(kind='pathway', …)` | the campaign **leaderboard** across candidates | ranked TOON |
| `edit` config + re-`put` | pull a lever (element/dopant/facet/reagents/fidelity) and retry | — |

Deferred: `view='mermaid'` (diagram-as-text, for a human).

---

## What each call actually returns

### `put(mode='preview')` — argue before you compute (no ML)

`put(kind='pathway', id='no-nh3-pd', mode='preview', text='substrate: "NO"\ntarget: "NH3"\nnetwork: branching\nslab: {element: Pd}')`

```
NO → NH3 on Pd(111) — network preview (branching), NO COMPUTE

intermediates:
{name	formula	composition}
NO	NO*	N1O1
NO+H	NOH*	H1N1O1
HNO	HNO*	H1N1O1
N	N*	N1
NH	NH*	H1N1
NH2	NH2*	H2N1
NH3	NH3*	H3N1

steps (atom-conserving → get NEB barriers):
{step	reactant	product}
r1	NO	NO+H
r2	NO+H	HNO
r3	HNO	N
r4	N	NH
r5	NH	NH2
r6	NH2	NH3

supply links (no barrier):
{from	to}
NO	NO+H

No energies yet. Argue with the network, edit the config, then re-put to run.
```

The LLM can now object *before* spending compute: "HNO→N direct dissociation is
unlikely; add the NOH branch" → edit config → re-preview.

### `get(view='intermediates')` — states + energies (post-compute)

Relative to the substrate root, ± = pooled across seeds×models, `conf` from the
low-confidence flag.

```
{name	formula	rel_eV	std	conf}
NO	NO*	0.000	0.02	ok
NO+H	NOH*	-0.31	0.05	ok
HNO	HNO*	-0.28	0.04	ok
N	N*	-0.95	0.18	low
NH	NH*	-1.42	0.09	ok
NH2	NH2*	-1.88	0.07	ok
NH3	NH3*	-2.05	0.05	ok
```

### `get(view='steps')` — elementary steps + barriers

```
{step	reactant	product	Ea_eV	Ea_std	dE_eV	conf}
r1	NO	NO+H	0.74	0.06	-0.31	ok
r2	NO+H	HNO	0.52	0.05	0.03	ok
r3	HNO	N	1.21	0.42	-0.67	low
r4	N	NH	0.63	0.08	-0.47	ok
r5	NH	NH2	0.58	0.06	-0.46	ok
r6	NH2	NH3	0.71	0.07	-0.17	ok
```

### `get(view='analysis')` — THE objective (what the optimiser reads)

The whole point. One headline + a barriers-ranked table + selectivity + honesty.

```
NO → NH3 on Pd(111)  (mace-mp, 3 seeds × 1 model = 3 samples)

rate-limiting: HNO → N   Ea = 1.21 ± 0.42 eV   [LOW CONFIDENCE]
  → gated by a barrier whose spread (0.42) exceeds tolerance. Escalate r3 to a
    higher fidelity before trusting this number or acting on it.
span (NO→NH3): 3.26 eV downhill; thermodynamic sink NH3* at -2.05 eV (favourable)

barriers (descending):
{step	reaction	Ea_eV	std	conf}
r3	HNO→N	1.21	0.42	low
r1	NO→NO+H	0.74	0.06	ok
r6	NH2→NH3	0.71	0.07	ok
r4	N→NH	0.63	0.08	ok
r5	NH→NH2	0.58	0.06	ok
r2	NO+H→HNO	0.52	0.05	ok

selectivity:
{branch	entry_step	entry_Ea	verdict}
reduction→NH3	NO→NO+H	0.74	target path
oxidation→NO3	NO→NO+O	0.44	COMPETING — kinetically favored (lower entry)

⚠ reduction to NH3 is NOT selective here: oxidation entry (0.44) < reduction
  entry (0.74). Lever idea: suppress O* availability (dry feed / O-scavenger).
```

### `get(view='warnings')` — where to distrust the numbers

```
{kind	where	detail}
low_confidence	N (state)	std 0.18 eV > tol
low_confidence	r3 HNO→N	Ea std 0.42 eV > tol
neb_not_converged	r3 seed=2	hit max_steps; barrier is the last (refined) estimate
```

### `search(kind='pathway', …)` — the campaign leaderboard

The cross-candidate read the optimiser ranks and picks the next lever from.

```
{pathway	surface	lever	rate_Ea	std	conf	status}
no-nh3-pd111-cu	Pd(111)+Cu	dopant:Cu	0.86	0.09	ok	ready
no-nh3-pt111	Pt(111)	element:Pt	0.98	0.11	ok	ready
no-nh3-pd100	Pd(100)	facet:100	1.03	0.12	ok	computing
no-nh3-pd111	Pd(111)	baseline	1.21	0.42	low	ready
no-nh3-pd111-s	Pd(111)+S	poison:S	1.55	0.14	ok	ready
```

---

## The optimisation loop, in these calls

An `LLM:*` planner coroutine (a project todo) driving the surface:

1. `put(mode='preview')` → sanity-check the network; argue; edit if wrong.
2. `put()` → run (routes to the GPU node); `auto_check` waits for
   `derived_job_succeeded`.
3. `get(view='analysis')` → read the rate-limiting Ea + selectivity + conf.
4. **If low-confidence** → bump fidelity (EMT→MACE→FAIRChem→DFT) on the gating
   step and re-run before acting (honest-uncertainty as a strategy).
5. **Ground the next lever** in the corpus: `search(kind='paper', queries=['Cu
   promotion Pd NO reduction'])` → propose dopant/facet/poison/reagent change.
6. `edit` config → re-`put`. `search(kind='pathway')` ranks the leaderboard.
7. Best candidate + its cited rationale → write into the project `draft`.

Each row of every table is a fact the LLM can contest, cite, or act on — no
picture in the loop.

## The `compare` view (LOCKED — built)

**One row per candidate; the reaction coordinate is the columns.** Chosen
because the loop's core op — rank candidates, and read "where did the lever
act" — is a column scan, which LLMs do reliably; candidates (which *grow*) are
rows (sortable, top-N), steps (bounded) are columns.

Rules that make it coherent:
- **`‡` columns hold the barrier Eₐ directly** (not cumulative TS energy) — a
  cell *is* the number you compare. **State columns hold relative energy.**
- **Supply bridges** (`+O*`/`+H*`, no barrier) get no `‡` column — the two
  states sit adjacent.
- Always-present summaries: **`RATE`** (max single-step Eₐ, the rate-limiting
  step) and **`SPAN`** (whole-path apparent barrier — the energetic span; can
  exceed every single step when a deep well precedes a high TS). Rows sorted by
  `RATE` ascending (best first). Precomputed — never make the model take a max.
- **Degrades gracefully:** candidates that don't share a network drop the
  state/`‡` columns → scalar leaderboard (`cand/lever/RATE/SPAN/conf`). Rows are
  always candidates.

Real output (`get(kind='pathway', id=…, view='compare')`, EMT, Pd vs Pt):

```
# ‡ = step barrier Eₐ; state cols = rel eV vs root.  ‡2 NO+O→NO2  ‡4 NO2+O→NO3
{cand	lever	NO	NO+O	‡2	NO2	NO2+O	‡4	NO3	RATE	SPAN	conf}
no-no3-pt	Pt	+0.00	-0.19	0.19	-0.53	-0.55	0.05	-0.57	0.19	0.19	ok
no-no3-pd	Pd	+0.00	-0.14	0.22	-0.40	-0.36	0.10	-0.27	0.22	0.22	ok
```

Invocation (v1): `get(view='compare')` on any computed pathway compares it
against every computed sibling sharing the same substrate→target. (A
`campaign:` tag scope can refine this later.)

## Build notes

- Views serialise via `precis.format.toon.dump(rows, schema=[…])` — pinned
  column order, TAB sep, braced header. Consistent with `search`.
- `analysis` is pure graph analysis over the stored `graph_json` (rate-limiting
  = max Ea on the substrate→target path; selectivity = compare branch entry
  barriers). No recompute.
- `preview` / `intermediates` / `steps` already have their data
  (`network_topology`, `results_json`); this slice is the TOON *rendering* +
  the `analysis` computation + the `precis-pathway-help` skill.
- Levers beyond today's config (dopant / facet / poison / pH) are catpath
  extensions added incrementally; the surface above doesn't change when they
  land — only the `lever` column gains values.
