# precis-mcp — Open Items

Durable backlog. Replaces the per-issue gripe trail (gripes 3667 +
3681 retired 2026-05-02 after the seven-verb surface refactor closed
their original framing) with a single canonical entry-point.

The mcp-critic review at
[`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md)
remains as the historical observation log; this file tracks only
what's still open.

> **Convention**:
> - **Status**: `open` / `blocked` / `deferred` / `done`
> - **Severity**: `critical` (blocks release) / `feature` / `polish`
> - **Owner**: rough estimate of where the fix lives
> - **Test**: name of the regression test that pins it (when fixed)

---

## 📜 Patent freedom-to-operate authoring loop — first live run (2026-07-16)

Shipped + **deployed** this session (main `147a984f`, all four cluster nodes):
the full **patent authoring loop** — sweep prior art → ingest patents → iterate
the description into patent lingo → write claims against a *comprehensive* view
of others' claims (independents verbatim, dependents grouped under them) →
record scoping decisions in a `plan` ledger → export USPTO-style with prior art
cited **in-text, no bibliography** (reader shows the same text, WYSIWYG). Slices
1–6 + per-authority citations + auto-include-our-claims + the auto-invoke hook +
plan-ledger injection + claim-family grouping are all on main. Design-of-record:
[`docs/design/patent-authoring-loop.md`](docs/design/patent-authoring-loop.md)
(complements `patent-drafting-merge.md`, the static genre). Key files:
`handlers/_patent_claims.py`, `handlers/_patent_ingest.py`, `handlers/patent.py`,
`workers/patent_digest.py`, `export/_patent_cite.py`, `export/{latex,docx}.py`,
`utils/prompt/predicates.py` (`is_patent`/`has_plan`), `workers/planner_prompt.py`.

- **Validate the loop end-to-end on a real draft** *(feature, open — first live
  run; this is a verification action, not code).* Everything is live and waiting
  for its first `doc_type=patent` draft. Create one via the web "**+ New draft →
  Patent application**", give it an `LLM:opus` planner todo, and watch a tick:
  (1) sweep + **ingest** the prior-art patents it cites (needs
  `PRECIS_PATENT_RAW_ROOT` + EPO OPS on the executor host — patent ingest is
  gated); (2) iterate the description toward patent lingo; (3) write claims with
  the freedom-to-operate `working_set` in view (prior-art independents verbatim,
  our claims so far verbatim); (4) log a scoping decision to the project `plan`;
  (5) **export** — confirm prior art renders in-text with no `\printbibliography`
  and the reader shows the same text. Watch for the patent-ingest gate on the
  agent-profile host and the surname-extraction heuristic on any non-comma
  bylines. Owner: end-to-end (web → plan_tick → export). Test: manual first-run.
- **~101-patent claim-marking backfill** *(polish, deferred — completeness
  only.)* New ingests self-mark claim blocks (`patent_block`); already-ingested
  patents predate the marker, so their claims won't appear in the FTO digest
  until re-swept. A backfill needs the cluster raw-XML/OPS to re-parse claim
  structure. Not blocking — the loop works on freshly-swept prior art. Owner:
  `handlers/_patent_ingest.py` + a one-shot backfill pass.
- **Slice 7 — visual claim tree-eye + interactive web claims view** *(feature,
  deferred.)* The FTO digest today is a `working_set` (reader eyes) injected into
  the planner prompt — text, not a rendered tree. A full visual claim-family tree
  and an interactive `/patent/<slug>` claims browser need new render + route
  surfaces. Deferred with the *why* recorded in the design doc. Owner:
  `precis_web/routes/` + a claim-tree renderer.

## 🎧 Daily audio casts — follow-ups (2026-07-15)

Shipped this session: the daily **reading-brief** (morning, `bm_george`) +
**nidra** (evening, `af_nicole`) audio casts. Both **compose with `claude-opus`
on `claude_inproc`/melchior** (where the litellm proxy lives); **TTS is the
separate downstream `cast_audio` pass on spark** (container Kokoro → the podcast
feed). Watches installed in prod (6am brief, 9pm nidra) and the whole loop was
proven autonomous end-to-end. Key files: `reading/{cast_common,briefing_cast,
meditation}.py`, `workers/cast_audio.py`, `workers/job_types/{reading_brief,
meditation}.py`, `cli/cast.py`. Skill: `precis-audio-help`.

- **Cast length calibration — verify + tune.** *(polish, open — fix deployed,
  unverified.)* The 2026-07-15 nidra rendered **~18 min vs a 45-min budget**;
  `_compose_long` asked opus for open-ended passages with no length target, so it
  under-wrote. Fixed in `ae37657a` (the word budget is now split across the
  segmented calls and each prompt asks for ~N words; `_CONCEPTS_PER_SEGMENT`
  6→4). **Not yet measured** — opus may still under-write the per-segment target;
  measure tomorrow's nidra and, if short, raise the target or add an
  over-provision factor. **The morning brief** also came out **~4 min vs its
  15-min target**: the single-call compose has *no* length floor and is
  content-bound (thin lanes → short brief). Decide: enforce a floor on the brief
  vs accept content-driven length. `wpm=110` is measured-accurate — leave it.
  Owner: `reading/{meditation,briefing_cast}.py` + `cast_common.word_budget`.
  Test: none yet.
- **Wire the quest lane into the morning brief** *(feature, open; also todo
  td161129).* `briefing_cast._lane_quest` is a degrade-to-empty stub; quest
  slice-1 landed on main (quest kind + `serves` + `quest_log` logbook), so the
  lane can now surface per-active-quest momentum + recent deeds (milestone /
  dead-end entries). The nidra could also bias its concept walk toward
  active-quest concepts (`build_meditation(bias_active_quests=)`, quest slice-2,
  dark until reading-prep slice 3). Owner: `reading/briefing_cast.py`.
- **Booklet (reading) lane** *(feature, blocked on reading-prep slice 2).*
  `briefing_cast._lane_reading` is a stub; lights up when the weekly booklet
  synthesis exists.
- **Test-artifact cleanup** *(polish, open).* Prod holds test cast drafts
  (`cast-nidra-test-546c21`, the suppressed opustest 161197) and the Qwen test
  episode `nidra-test-546c21` is still on the feed. Remove when convenient.
- **Cast-draft corpus hygiene** *(polish, open).* Daily cast drafts (`kind=
  'draft'`, `meta.cast`) accumulate and are embedded + searchable like any draft.
  Consider `meta.no_index` and/or a retention GC (as `agentlog` has) so daily
  narration scripts don't pollute the corpus over time.
- **gripe 161169 — RESOLVED** (`edc99a1d`): compose couldn't reach an LLM on a
  data node (litellm is melchior-loopback-only); fixed by moving compose to
  `claude_inproc`/melchior with `claude-opus`. Recorded here for the trail.

---

## 🗺️ Quest layer (design-of-record `docs/proposals/quest-layer.md`)

The aim-layer above projects/streams/concepts. Slices 1 (read-only
structure — `quest` kind + `serves` + logbook + tree rollup, main
`2ce51f5f`), 2 (reweighting — priority down the `serves` DAG into
rotation/acquisition/reading, `src/precis/quest/reweight.py`, main
`8a61716f`), 3 (gaps + health — `src/precis/quest/gaps.py`,
`view='gaps'`/`id='/gaps'`), and **all of slice 4** (rungs 4a–4e: dossier + research tick —
`tick.py`/`dossier.py`, migration 0067; compute dispatch + Pareto frontier —
`compute.py`/`frontier.py`; local↔frontier cascade — `cascade.py`; allocator —
`allocator.py`; graduation — `graduate.py`) are **shipped, not deployed**. Skill
`precis-quest-help`; tests `tests/test_quest*.py` (7 files). **The whole quest
layer is built — the only steps left are operational (deploy + link real
quests + flip `PRECIS_QUEST_LOOP_ENABLED`).**

**Operational — do these to make the shipped slices actually steer:**

- **Deploy slices 1+2+3+4a+4b to the cluster** — `open` / `feature` / owner
  `/go` → `scripts/deploy`. All ship dark *behaviourally* (a no-op until a quest
  exists; the tick is manual-only until 4d, and compute is `--compute`-opt-in),
  so deploy is low-risk. Migration 0067 auto-applies on deploy. NB real relax
  needs the `struct_relax` GPU lane live on spark to actually run — otherwise a
  dispatched sim just queues. Finder: Opus session.
- **Link 3–4 mission quests to real projects** — `open` / `feature` /
  owner prod-data (MCP `put(kind='quest')` + `link(rel='serves')`). Derive
  the strivings from `docs/mission.md` + the live research programs (NO→NH₃
  catalyst, …) and link the existing project todos to them. **A prod-data
  step — do it after deploy** so the `quest` kind is live. Until this lands,
  the reweighting is a no-op in prod (nothing serves anything). This is the
  last slice-1 deliverable. Finder: Opus session.

**Slice 3 — gaps + health** (`feature`, `src/precis/quest/gaps.py`):
**BUILT + shipped** (not deployed). The striving exposes its own
exploration queue — **thin-support** (little serves it), **no-literature**
(work under way with no `paper` grounding), **low-mastery** (a served
`concept` below the mastery floor), **open-hypothesis** (a `hypothesis`
logbook entry with no later `result`/`dead-end`) — plus **health**
(momentum label + an embedding alignment floor) on the tree rollup.
Surfaced in `view='tree'`, `view='gaps'` (per quest), `id='/gaps'`
(corpus-wide). Read-time, mechanical, no-op until servers exist. Tests
`tests/test_quest_gaps.py`. *Residual for slice 4:* the alignment floor is
mechanical only — the **dream re-review** that re-scores stale/low-cosine
`serves` edges + writes a verdict onto each edge (human override wins) is
deferred to slice 4; "no-supporting-paper for a specific claim" is
approximated here as quest-level no-literature (no per-claim `citation`
grounding check yet).

**Slice 4 — the autonomous research loop** (`feature`, the big one), built
as five dark rungs:

- **4a — dossier + tick skeleton** — **BUILT + shipped** (not deployed).
  `dossier-of` relation (migration 0067) + a `draft` the quest owns (the
  rolling context, `view='dossier'`); `src/precis/quest/tick.py`
  `run_quest_tick` = one in-process **structured** step through the ADR-0046
  seam that reads the rolling context (statement + dossier + slice-3 gaps +
  momentum + logbook tail) and returns logbook entries + a whole-rewritten
  dossier. Logbook write unified into `src/precis/quest/logbook.py` (shared
  with the handler). CLI `precis quest tick|dossier|gaps`. Dark: nothing
  auto-mints a tick (that's 4d); `PRECIS_QUEST_LOOP_ENABLED` gates the future
  auto-dispatcher. Tests `tests/test_quest_tick.py`.
- **4b — compute dispatch + proposer + Pareto frontier** — **BUILT + shipped**
  (not deployed). `src/precis/quest/compute.py` + `frontier.py`. The tick emits
  **proposals**; a candidate carrying an atomistic `structure` spec becomes a
  content-addressed `structure` that `serves` the quest (`candidate`-tagged),
  and — with `compute=True` (`precis quest tick --compute`) — its relax
  dispatches on the GPU node via the derived lane (NO `requested_by`, since a
  quest never closes). Harvest reads converged runs → `result`+`cost` entries
  (idempotent via `meta.quest_harvested_upto`); a failed relax job →
  `ruled-out:relax-failed` tag + `dead-end` entry. `quest_frontier` = Pareto
  over candidate measures (default minimise energy; `meta.rubric_objectives`
  override), `view='frontier'`. Real relax is a defensive wrapper (degrades on
  error) monkeypatched in tests. Tests `tests/test_quest_compute.py`.
  *Residuals:* the **proposer** is just the tick's model grounded in the dossier
  — frontier-seeded *directions* land in 4c; turning a prose rubric into the
  objective vector is still the `energy`-default + meta override (open Q3);
  `pathway` isn't a target (catpath plugin not in-tree).
- **4c — the local↔frontier cascade** — **BUILT + shipped** (not deployed).
  `src/precis/quest/cascade.py`. A tick runs at the local/cheap tier by default;
  `escalation_signal` fires a **frontier review** (`CLOUD_SUPER`, senior-reviewer
  prompt with the Pareto frontier in-context, sets `directions` logged as a
  `decision`) on **first-review** / **new-evidence** (≥`FRONTIER_REVIEW_EVERY`
  new `result` entries) / **stalled** (`STALL_TICKS` since last improvement).
  `update_cascade_state` maintains counters + the **promise** proxy
  (frontier-improvement rate = objective gained / recent cost) for 4d.
  `run_quest_tick(review=None|True|False)`; outcome carries `escalated`/`mode`.
  Tests `tests/test_quest_cascade.py`. *Residual:* "surprise" isn't a distinct
  signal (folded into new-evidence/improvement); rubric→objective still the
  energy-default.
- **4d — the allocator** — **BUILT + shipped** (not deployed).
  `src/precis/quest/allocator.py`. `pick_next_quest` ranks active quests by an
  EWMA bandit (`base_weight × momentum × (1+promise)` + `exploration/(picks+1)`);
  `run_allocator_pass` (gated `PRECIS_QUEST_LOOP_ENABLED`, agent-profile worker
  pass in `cli/worker.py` + `precis quest run [--force] [--budget N]`) cools the
  cold + picks + ticks (compute=True) + folds the EWMA. Weekly proportional
  budget (`PRECIS_QUEST_WEEKLY_BUDGET`, unset=uncapped) vs `weekly_spend`
  (7-day tote); `cool_stalled` → `dormant` + `reflection`. Cost/credit-overlap
  (Q1) resolved by construction (candidates content-addressed per quest → billed
  once). Tests `tests/test_quest_allocator.py`. **THIS is the dark→live switch:
  set `PRECIS_QUEST_LOOP_ENABLED` on the melchior agent worker to run the loop.**
- **4e — ceiling awareness** — **BUILT + shipped** (not deployed).
  `src/precis/quest/graduate.py`. A quest declares its ceiling in
  `meta.graduation` (`{key, sense, threshold}`); `graduate_frontier` tags a
  frontier candidate that crosses it `needs-experiment` + logs a `milestone`
  (deed), idempotently. Slice-3 gaps surface it as a `needs-experiment` item; a
  ★ marks it in `view='frontier'`. Wired into `run_compute_step`. No rule →
  no-op (dark until a quest opts in). Tests `tests/test_quest_graduate.py`.

**Deferred within slice 2 (reweighting):**

- **Dream nomination-*prompt* tilt** — `deferred` / `feature` / owner
  `workers/dream_agent.py` + `data/prompts/dream-prompt.md`. Inject active-
  quest context so the dream *reasons* about which papers/threads to
  nominate (fisheye eye-draw boost + a "## Active quests & their needs"
  prompt block). Deferred because the dream agent is **gated off in prod**
  (`PRECIS_DREAM_AGENT` unset); tilting the *live* `fetch_oa` backlog (done)
  covers the acquisition half that actually runs. Wire when the dream is
  turned on. Finder: Opus session.

**Open questions from the proposal (resolve as the steering rungs land):**

- **Cost & credit attribution under overlap** *(slice 4, the sharp one)* —
  priority *pulls* by max, but a sim serving two quests burned one sum of
  GPU; attributing it fully to both double-counts the weekly budget. Pull =
  max; cost/credit need a conservation rule (split, or shared pool). Does a
  shared breakthrough boost both quests' EWMA? (Likely yes for credit, no
  for cost.)
- **"Promise" is the softest bid term** *(slice 4)* — the EWMA bid is
  `priority × momentum × promise`; *promise* (expected remaining
  improvement) needs a concrete proxy (frontier-improvement rate, result
  variance, untried-candidates-near-front).
- **Prose rubric → machine-measurable objective** *(slice 4)* — turning a
  quest's success criteria into a computed score vector the loop optimises.
  Realistic path: frontier model judges qualitatively first; hard numbers
  as sim outputs get parsed.
- **The proposer is the crux and least-specified** *(slice 4)* — the loop is
  only as good as "propose the next candidate"; needs grounding (dossier +
  literature + frontier neighbours) with the frontier model seeding
  directions.
- **Sub-quest vs achievable-goal boundary** *(craft)* — rule of thumb
  captured in `precis-quest-help` (open-ended "best/a …" → quest;
  completable deliverable → a project that `serves`). Revisit if authors
  keep getting it wrong.

**Standing leans (decided-enough, easy to flip):** dossier = a `draft` the
quest owns (arrives with the loop, slice 4); alignment judge = the
embedding-proximity floor (**built, slice 3**) + a dream re-review that
scores stale/low-cosine `serves` edges, human override wins (**deferred to
slice 4** — cadence/storage/verdict-on-edge). Health (momentum + the
alignment floor) is now computed on the `view='tree'` rollup (slice 3).

---

## 🧪 chem-tools (ADR 0056) — remaining slices + live-verification (2026-07-15)

The `route` retrosynthesis kind (precis-chem plugin) ships **dark** behind
`PRECIS_CHEM_ENABLED`. Slices 1–3 are built; slice 1 is live on spark (aizynth).
Design-of-record: `docs/design/chem-tools-integration.md`. Backlog:

- **Deploy slice 2 (LinChemIn normalize).** · *feature* · Rebuild the aizynth
  image on spark so the shim emits `route.json` (metrics + engine-agnostic
  steps): `cd ~/work/cluster && ansible-playbook playbooks/43-aizynth.yml`. Until
  then the live aizynth engine uses the slice-1b `trees.json` fallback (no
  descriptors). Also run `scripts/deploy` for the precis-side `parse_syngraph` /
  `view='metrics'` code. Owner: `~/work/cluster/roles/aizynth`, `docker/aizynth`.

- **ASKCOS (slice 3) live-verification.** · *feature* · Slice 3 is built +
  gate-tested with stubs, but **inert in prod** (needs a running ASKCOS v2 + the
  normalizer image). To go live: (1) stand up an ASKCOS v2 deployment, set
  `PRECIS_ASKCOS_URL`; (2) a `roles/normalizer` play to build the
  `precis-normalizer` image on the route node (mirrors `roles/aizynth`);
  (3) **verify the Tree-Builder request/response schema against the instance's
  `/docs`** — the one unverified surface, localized + flagged in
  `src/precis_chem/askcos.py` (endpoint `/api/tree-search/mcts/call-sync-without-token`,
  fields `smiles`/`max_depth`/`max_branching`/`expansion_time` → `result.paths`).
  Owner: `src/precis_chem/askcos.py`, `docker/normalizer`, `~/work/cluster`.

- **Slice 4a — `protein` kind (folding).** · *SHIPPED+DEPLOYED (main `f1a293d1`)* ·
  The `precis_bio` plugin: `protein` kind + `fold` job + `AlphaFold3Engine`
  (de-novo, **container** transport reusing slice 3's seam), gate-green (25
  tests). Grounded on the **real AF3 v3.0.1 install on spark** (image
  `alphafold3:ready`, GB10; memory: `alphafold-spark-facts`).
- **Slice 4b — folding live-run (deploy).** · *GREEN — end-to-end proven* ·
  `roles/alphafold` + `playbooks/46-alphafold.yml` (cluster repo `44f9242`):
  asserts the image + `af3.bin` weights, GPU passthrough, XLA cache, wires the
  `PRECIS_FOLD_*` worker env; topology `bio_fold:[spark]` + `bio_plugin:[melchior]`
  un-darks `PRECIS_BIO_ENABLED` on melchior. **Full prod dispatch PROVEN**: a real
  `put(kind='protein', engine='alphafold3')` via the deployed runtime minted fold
  job 161868 → spark worker claimed + ran AF3 (rc=0) → wrote back → protein 161867
  folded, pLDDT 84.7, pTM 0.1 (identical to the direct smoke); all three flagged
  unknowns (output naming, summary keys, de-novo result) resolved. Rootful dockerd
  mounts reto's world-readable models even though the deploy shell can't traverse
  `/home/reto`. **Hardening added** (mem-cap, this branch): `PRECIS_FOLD_MEM_LIMIT`
  → `--memory`/`--memory-swap` on the fold container (role default `96g`), since
  the GB10's unified CPU+GPU memory means an uncapped XLA spike could starve the
  worker. Note: drive plugin-kind puts via `runtime.dispatch` (the CLI/`repl` `put`
  has a curated arg allowlist that rejects `sequence`/`engine`).
- **Slice 4c — folding accuracy + structure convergence.** · *convergence BUILT* ·
  (1) **`structure` convergence — DONE**: `get(kind='protein', view='structure')`
  projects a fold's mmCIF into a non-periodic `structure` ref (`precis_bio/
  converge.py`, dependency-free `_atom_site` scan — NOT ASE, which is `[dft]`-gated)
  named `<slug>-fold`, linked via the asymmetric `has-fold-structure` relation, so
  it renders in the existing `/structure` 3D viewer. Content-slugged + idempotent;
  bonds inferred for small folds, atom-cloud for large. (2) **ColabFold MSA engine**
  — *needs-decision*: ColabFold is NOT a docker image and NOT on PATH on spark
  (unlike AF3); a reto-home conda install can't be run by the `deploy` worker.
  Clean path = containerize it (`colabfold:ready` image, like AF3) + decide the
  MSA source (MMseqs2 API vs local DBs). De-novo single-seq is lower accuracy
  (insulin A pTM 0.1 illustrates it).

- **Slice 5 — `sequence` kind (design).** · *feature* · ProteinMPNN / RFdiffusion
  as another `job_type`, GPU on spark. Sibling of slice 4; same "decide the
  engine + install role" caveat.

- **Slice 6 — ChemCrow / agentic.** · *BUILT* · The `precis-lab-help` skill —
  the composition layer, not a framework: canonical recipes chaining
  `route`/`protein`/`structure`/`paper` into a research loop, for an interactive
  agent or an autonomous `plan_tick`. Indexed in `precis-toolpath-help` +
  `precis-overview`. **Deferred**: a dedicated chem/bio `plan_tick` executor that
  auto-drives the loop (couples to the planner; the skill already lets the
  generic planner do it).

- **Slice 5 — `sequence` design (LigandMPNN) + 4c fold accuracy (Boltz-2).** ·
  *foundation PROVEN — ready to build* · Engines chosen (2026-07-16): **Boltz-2**
  (MIT, PyTorch, hosted MSA — no 2TB DBs) as a new `protein` engine; **LigandMPNN**
  (MIT, ligand-aware structure→sequence) as the new `sequence` kind + `design`
  job. **PyTorch-CUDA foundation = SOLVED (no blocker):** stock `pip install torch
  --index-url https://download.pytorch.org/whl/cu128` gives torch 2.11.0+cu128 with
  working GPU on the GB10 (`is_available=True`, capability `(12,1)`=sm_121, a real
  matmul ran) — NO NGC container/creds needed. (The old "AF CUDA fix" was JAX, a
  separate stack; only CPU torch existed, hence the earlier false blocker.) BUILD
  PATH: a `torch-cuda` base image (python + pip torch cu128; the wheels bundle the
  CUDA runtime, so a slim base + `--gpus all` should suffice) → Boltz-2 + LigandMPNN
  layer `FROM` it (weights from HF, reachable+fast), each = a precis engine adapter
  + a `roles/*` mirror of `roles/alphafold`. Order: base → Boltz-2 (extends the fold
  seam) → LigandMPNN (new kind + job). (Aside: AF3 itself supports ligand co-folding
  via SMILES/CCD — reto's `alphafold3_ligand_findings.md` — a separate capability.)

- **MCP-surface design review — chem/bio kinds.** · *design-review, filed* ·
  Review how `route`/`protein`/`structure`/(future `sequence`) present through the
  seven verbs as a *coherent* surface: consistent `view=` naming + params across
  the compute kinds; discovery of dark/plugin kinds by an agent; the **CLI/`repl`
  `put` arg-allowlist gap** that rejects plugin kwargs (`sequence`/`engine`) so
  only `runtime.dispatch`/the MCP JSON-RPC can drive a plugin-kind `put` (surfaced
  during the 4b live smoke); whether the `precis-lab-help` composition layer is
  the right agent on-ramp. Its own focused pass, not a squeeze-in.

- **Plugin-relation read-time inverse (gripe 160213).** · *FIXED* ·
  `Store.inverse_relation` now reads `relations.inverse_slug` from the DB (cached
  like `valid_relations`), and `links_for` uses it — so an asymmetric plugin
  relation's inverse mirrors on read. `_INVERSE_RELATIONS` stays as the built-in
  typo-safety reference only. Proven by `precis_bio`'s `has-fold-structure` /
  `fold-structure-of` pair (the first asymmetric plugin relation, slice 4c).

---

## 💰 Budget guardrails — global spend circuit breaker (2026-07-15)

Design-of-record: [`docs/design/budget-guardrails.md`](docs/design/budget-guardrails.md).
precis has solid per-*call* caps (`claude_p` $0.10, `claude_agent` $2.00,
`plan_tick` $5.00) and a full cost ledger (`llm_call_log`, `ref_events.cost_usd`,
`cache_state.cost_usd`) but **no aggregate ceiling** — a tight loop of cheap
calls or many workers at once is an unguarded runaway-budget risk. Ship
lightweight, loose guide rails + a hard backstop, never blocking interactive
work. Status:

> **Devin-merge review residuals — fixed on branch `worktree-robust-sauteeing-volcano`
> (2026-07-16, unshipped).** Closes gripe **161849** + others from the
> `e29b18a9` review: (1) the breaker now gates **every paid tier** (any non-`free`
> band, cheap `CLOUD_MID`/`CLOUD_SMALL` included) — Reto's call, "if it costs
> money the cap limits it" — via `bands.is_paid` + `breaker.gate_tier`/`gate_paid`
> (fetches gate on any non-zero estimate); `/budget` UI copy made honest. (2)
> **Real-cost capture** wired: `LlmClient.complete` reads OpenRouter's
> `usage.cost`, `result_from_openai` prefers a provider-returned `cost_usd` over
> the token table, and `qwen-heavy` dropped from `PRICE_TABLE` (it's the free
> `LOCAL_BIG` band). (3) The two **enforcement seams** now have integration tests
> (`router.dispatch` → error `LlmResult` on trip; `_fetch_guarded` → `Upstream`,
> skips `_fetch`). Also this branch: OSS `result_from_openai` parses the trailing
> JSON block into `LlmResult.data` (gripe 159758) and the tool-less OSS agent
> path advertises **no** tools when `mcp_config is None` (gripe 159759). Still
> open below: Piece B's `/budget` web-editable caps + Discord alert are the
> larger design; the minor "meter fails open on a metering error" note from
> 161849 is left as-is (defensible). *Perplexity real-cost still uses a flat
> ClassVar — unchanged here.*

- **Piece A — cost-band affordance** — `open` / `feature` / owner
  `src/precis/budget/` + `utils/llm/router.py` + `_cache_base.py`. Uniform
  `free · cheap · expensive` (+ `fast · slow`) words surfaced to the model
  (decision: `expensive`, not `steep`), paired with a permissive "escalate
  freely when needful" policy line. No enforcement — pure sense.
- **Real-cost capture** — `open` / `feature` / owner `utils/llm/router.py`
  (`result_from_openai`) + `workers/llm_summarize.py` + `handlers/perplexity.py`.
  The tote must sum the provider's *actual* returned cost, not estimates.
  Claude already reports true `total_cost_usd`; the OSS/local + OpenRouter path
  drops `usage` (needs a tokens→$ price table or OpenRouter's `cost` field);
  perplexity uses a flat ClassVar (needs the response `usage`/`cost`). The
  per-call `$0.10/$2.00/$5.00` caps are *ceilings*, never the tote's input.
- **Piece B — global circuit breaker (hourly + daily)** — `open` / `feature` /
  owner `src/precis/budget/breaker.py` + `router.dispatch` + cache `_fetch` +
  `/budget` web page. Two web-editable numbers, `PRECIS_BUDGET_HOURLY_USD` +
  `PRECIS_BUDGET_DAILY_USD`, bounding router LLMs (claude + OpenRouter) **and**
  paid fetch kinds (perplexity, …) together. On trip: refuse new *expensive*
  work (graceful `LlmResult.error`), auto-clear as the rolling window ages off,
  emit an `alert` routed to **Discord** via the existing alert→news channel;
  cheap/free/interactive always flow. Rolling tote + by-model + by-source
  breakdowns shown on the status page.
- **Piece C — quest attribution (later)** — `deferred` / `feature`. Just let
  `LlmRequest.source` carry a quest id so per-quest spend *views* are a query
  over the same ledger when the quest layer lands. A global breaker needs zero
  attribution math — sidesteps the quest-overlap double-count (quest-layer
  open question #1).
- **Open decisions remaining** (see design doc): ledger union without
  double-count (`llm_call_log` vs `ref_events`); per-model price-table source +
  upkeep; cheap-band threshold; real cap defaults (tune from observed spend).

---

## 🩹 Residuals — asa storeless-precis incident (2026-07-14)

The 2026-07-14 investigation ("asa can't file gripes") root-caused a
double-build in `precis serve` (fixed in `8b07c0ad`; the boot build scrubs
`PRECIS_DATABASE_URL`, so `tools/core` lazily built a *second*, storeless
runtime that served every MCP tool call). Fixes shipped: the runtime-share
(`8b07c0ad`), the boot-time connect retry (`4c47a652`), asa's health-check
that detects a degraded precis (asa-bot `2727054`), and the asa deploy
ssh-agent fix (cluster `d86f8c6`). asa-bot was also **folded into this
monorepo** as a sibling package (`src/asa_bot`, `[asa]` extra, `asa-bot`
entry point — `12cc38d0` + `f7de0f14`; cutover deployed, asa runs
`precis-mcp[asa]` self-contained on its own venv). Status:

- **`build_runtime` is storeless-after-scrub by construction** — `done` /
  `polish` / owner `runtime.py` + `secrets.py`. `build_runtime()` now falls
  back to the adopted process store's DSN when `PRECIS_DATABASE_URL` is
  scrubbed from `os.environ` (`secrets.get_adopted_dsn()`). Test:
  `test_build_runtime_falls_back_to_adopted_dsn_after_env_scrubbed`.
- **conv capture silently stopped 2026-06-27** — `open` / investigate /
  owner `asa-bot capture_shim` + `precis handlers/conv`. No `kind='conv'`
  rows in prod since 2026-06-27 despite `POST /capture` returning 200 and
  no `capture-fallback.jsonl` on disk. Very likely the same storeless-precis
  root cause (capture routed through the degraded tool runtime) — **verify
  after the next asa Discord turn** now that the double-build fix + the
  monorepo cutover are deployed; if still broken, trace the shim's precis
  write path (200 despite no persisted row). Finder: Opus session.
- **asa venv can't `import precis`** — ✅ **DONE** (fixed by the monorepo
  merge: asa now installs `precis-mcp[asa]`, so `precis.utils.db_log_handler`
  imports cleanly — verified asa-bot rows landing in `worker_logs`
  2026-07-14). No longer a residual.

---

## 🔐 secrets vault — SHIPPED + fully cut over (2026-07-13)

ADR 0055 / migration 0059. **DONE, deployed, validated on prod.** Encrypted
`vault.secrets` + `vault.list/mask/reveal/set_secret/delete_secret`, resolver
`src/precis/secrets.py` (env→vault→file, cached), `precis secret` CLI, `/secrets`
web editor (in the Ops nav), `requires_secret` kind gate, DSN scrubbed from
subprocess env at boot. Every precis/asa leaf secret (13) migrated to the DB
vault and **removed from ansible-vault + env** — the DB vault is the exclusive
source; ansible-vault is down to postgres bootstrap + non-precis infra. feynman
+ quest retired in the same pass (redundant / dormant).

Remaining (small):
- **`/secrets` web smoke test** — `polish`, Owner: `tests/`. FastAPI TestClient
  test (list renders, set writes, blank submit no-ops). The route is only
  covered by app-boot import today.
- **Left in env by design**: `PRECIS_UNPAYWALL_EMAIL` (a mailto, not a
  credential); the litellm/openclaw ansible-vault secrets stay until those tools
  retire — sweep with the litellm teardown.
- **Deferred by design (ADR 0055)**: per-service DB roles (`precis_secrets` /
  `precis_web` / `asa`) + per-name ACL; `pg_notify`-driven cache invalidation
  (currently a 60s TTL); out-of-process extension broker.

### Feynman gleanings (from the retirement review — feynman itself is gone)
- **(Medium) Cheap/local-model research tier** — precis's agent/research surfaces
  (asa, reviewers, planner, `perplexity-research` @ ~$0.50/call) all run cloud
  Claude with no cheap pre-filter. Add a local-model tier (ADR-0046 router
  `Tier.LOCAL_*`) feeding the existing search/research kinds for broad fan-out /
  low-stakes triage before paid escalation. NB feynman's substrate (litellm) is
  retiring, so it needs a fresh local-serving decision. NOT a standalone agent.
- **(Low) "Corpus before paid web" cost-ordering line** — `precis-research-help`
  frames the corpus as the substrate but never states "exhaust free corpus search
  before spending on `perplexity-research`." One line in that skill + asa's SOUL.
  The only nuance in feynman's `cluster-library.md` precis lacks.

### Cluster residuals surfaced during the secrets pass (ops, `~/work/cluster`)
- **balthazar `/opt/mcps` wiped every /go** — **FIXED** (`redeploy-precis.yml`
  step 3 no longer targets `scheduler`, and scopes the delete to `/opt/mcps/venv`;
  root cause db84485). Venvs restored; rss_ingest reveals S2 from the vault.
- **daily_briefing references a dead `cluster` DB** — pre-existing;
  `roles/daily_briefing` runs `psql -d cluster` (renamed to openclaw / retired),
  which fails the play. Non-fatal (tables already exist) but should be repointed
  at `precis_prod` or removed.
- **extract_watch uv-cache perm error on balthazar** — `~deploy/.cache/uv` has a
  root-owned `.git` blocking `uv pip install`; chown/clear it.
- **Orphan sweep from the feynman/quest retirement** — the installed venvs / npm
  bits still on the nodes (`/opt/mcps/quest`, `/opt/mcps/extract`, the
  `@companion-ai/feynman` npm global on melchior), quest's `papers` DB schema,
  and now-unused `quest_*` / `feynman` group_vars. All harmless orphans; sweep in
  a dedicated teardown pass alongside the litellm retirement.

---

## 🎨 `figure` kind — deferred slices (2026-07-12)

Slice 1 shipped: the `figure` kind (interactive SVG canvas), migration 0057,
`handlers/figure.py` + `precis/figure/{svg,turn}.py`, the `/figure` web editor
(draw-with-me turn loop, compile + out-of-bounds lints, sanitize, bounded
auto-heal), skills `precis-figure-help` + `precis-figure-svg`. All **feature
extensions**, not bugs — ordered roughly by value:

- **PNG / animated-raster export** — `feature`, Owner: a `figure_render`
  derived-lane job + a rasterizer. No SVG rasterizer is a dep today
  (cairosvg/resvg need system libs / rust — the reason it's deferred). Design:
  own the timeline as **declarative keyframes on named nodes**, interpolate +
  render each frame as static SVG via `resvg`, encode GIF/APNG/WebP — **no
  headless browser** (raw SMIL/CSS wouldn't survive export). Still PNG is the
  first step.
- **three.js / `scene3d` mode** — `feature`. One kind, `meta.render ∈
  {svg,scene3d}`; 3D uses a **declarative scene IR + trusted client renderer**
  (never eval raw three.js — XSS). Add `precis-figure-scene3d` skill.
- **Per-node chunk split** — `feature`, Owner: `figure/svg.py` + handler.
  Today the source is one `figure_node` chunk (the whole `<svg>`); split into
  one chunk per top-level element/group (`fn<id>` each) once per-node *edits*
  (batch transaction) land — the payoff that justifies the XML round-trip.
- **Draft-embedding** — `feature`. A draft includes a figure's rendered raster
  as an **asset** (not a document export — orthogonal to `corpus_role='none'`;
  reuse `export/sources.py`'s asset resolver). Add a `figure-in`→draft link.
- **`read(handle)` reference tool in the turn loop** — `feature`. Let the
  model pull any `dc…`/`fn…`/`pc…` handle into the turn (vocab-by-reference:
  "eyes like dc1234"). One read-only tool, on-demand into the variable layer.
- **Pin full `precis-figure-svg` skill text into the turn prompt** — `polish`,
  Owner: `figure/turn.py` + route. The turn currently inlines a *condensed*
  operating manual in `build_prompt`; wire the real skill body as the pinned
  cached layer (`run_turn(..., skills=…)` is already the seam).
- **Formalized-convention hard-checks** — `polish`. Optional opt-in: promote a
  *specific* formalizable convention (e.g. an explicit hex palette allowlist
  declared in the vocab) to a mechanical lint. Most conventions stay the
  model's job (held via the vocab), never a general "convention linter".

## 🖇️ `mermaid` kind + diagram chunk-binding (ADR 0057) — SHIPPED + LIVE (2026-07-16)

All five slices of ADR 0057 are on `main` (design-of-record
[`docs/design/diagram-editing-and-chunk-binding.md`](docs/design/diagram-editing-and-chunk-binding.md)):
element→chunk `depicts` bindings (migration 0064), the rich figure editing
environment, the shared `DiagramLang` core + `DiagramHandler` base
(figure/mermaid are two instances at every layer), the `mermaid` kind via
pure-Python `mermaidx` (migration 0066), and the `diagram_propose` autonomous
tick. **The `mermaid` kind is un-darked and LIVE** (deployed 2026-07-16,
main `c7ac23db` + cluster `2f1d2f3`; `mermaidx 0.8.3` verified on the serve +
worker venvs). Remaining follow-ups:

- **`~~Un-dark mermaid~~` — DONE (2026-07-16).** Dropped the
  `PRECIS_MERMAID_ENABLED` gate (register `MermaidHandler` unconditionally like
  `figure`), added `mermaid` to `[all]` + relocked, and added the `[mermaid]`
  extra to the cluster install specs (`roles/mcps` `[patent,mermaid]`,
  `roles/precis_web` `[…,mermaid]`, `roles/precis_worker` `[…,mermaid]`).
  Deployed via `scripts/deploy`. **Render-test coverage (clarified 2026-07-16):**
  CI (GitHub Actions `check.yml`) runs `uv run --extra all pytest`, and `[all]`
  includes `mermaid`, so the `importorskip('mermaidx')` compile/render tests
  **already run for real on every push to main** — the durable gate is covered.
  The only place they skip is the *local* `scripts/ship` `precis-dev` container,
  whose cached `precis-mcp:dev` image predates `mermaidx` in the lock. The
  Dockerfile is already correct (`deps` stage does `uv sync --all-extras`); a
  forced rebuild would cascade into an expensive model re-bake (`models` is
  `FROM deps`) for a test-set CI already runs — so leave it to self-heal on the
  next dep-driven `deps` rebuild rather than forcing it. NOT a coverage gap.
  Also consider a `websearch`-style env kill-switch if a fleet-wide off-switch
  is ever wanted (lean: no — figure has none).
  **Wheels are pure (no compiler): `quickjs-ng` + `resvg-py` cover
  macOS-arm64 + manylinux/musllinux, `resvg-py` is already pulled by
  `[docx]`.** Finder: Opus session.
- **`~~Intent-discovery skills~~` — DONE (2026-07-16).** Added a
  `precis-mermaid-<type>` skill family (flowchart, sequence, class, state, er,
  journey, quadrant, requirement, gitgraph, timeline, xychart, mindmap) so an
  intent like "org chart" / "database schema" / "sequence diagram" routes to
  `mermaid` via `search(kind='skill', q=…)`. Each is terse and defers CRUD to
  `precis-mermaid-help` / craft to `precis-mermaid`. One combined
  `precis-mermaid-unsupported` redirect skill covers the engine gaps below.
- **Engine gaps — gantt / pie / sankey / C4 / block don't render** — `bug`,
  Owner: `mermaid/mermaid.py` + `[mermaid]` extra. The in-process QuickJS engine
  lacks browser globals, so these mermaid types validate-fail: **gantt**
  (`offsetWidth`), **pie** (`structuredClone` undefined), **sankey-beta**
  (`not a function`), **C4Context** (`screen` undefined), **block-beta**
  (`circular reference`). Fix path: bump `mermaidx` when upstream ships a fuller
  QuickJS shim, evaluate `termaid`, or polyfill the missing globals
  (`structuredClone`/`screen` are cheap; `offsetWidth`/DOM layout for gantt is
  hard). Until then `precis-mermaid-unsupported` steers the model to renderable
  alternatives (timeline / xychart / draft table) so it doesn't try+fail.
  Finder: Opus session (2026-07-16 diagram-type sweep).
- **Bind on every node-bearing diagram type** — `feature`, Owner:
  `mermaid/mermaid.py`. ✅ DONE (Opus session 2026-07-16). `_diagram_kind()` now
  dispatches per-grammar extractors — flowchart/graph, sequence, class (decls +
  UML relations + `Foo : member`), ER (cardinality-op relations + attribute
  blocks), requirement (`requirement`/`element` blocks + `- verb ->` relations),
  state (`state` decls + transitions, `[*]` pseudo-states excluded), mindmap
  (indentation tree, explicit id or text-slug), gitGraph (branches + `id:`-tagged
  commits). Data-series types (journey / timeline / xychart / quadrant) and the
  engine-unsupported ones return `[]` (no stable node ids) instead of misparsing
  data rows. So `link(element=…)` + `lint_bindings()` no longer false-positive
  off-flowchart. Tests in `tests/test_mermaid.py` (pure extraction always runs;
  render-validated per type under `importorskip`).
  - Residual (deferred, not chased): the scan is still a pragmatic per-grammar
    approximation, not a faithful mermaid parser. Considered + rejected for now
    the "extract ids from the *rendered* SVG (authoritative)" option — it needs
    the `mermaidx` engine, but `elements()`/`lint_bindings()` must work on the
    dark gate where the engine is absent, so the source scan stays the primary.
    An SVG cross-check where the engine *is* present is a possible future
    refinement, not a gap. Mindmap ids for bare (id-less) text nodes are a
    text-slug, so renaming the text renames the id — acceptable (a rename
    breaking a binding is what the lint is for).
- **Rich cross-kind seed rendering in `diagram_propose`** — `feature`, Owner:
  `workers/job_types/diagram_propose.py`. `compose_message` inlines chunk-handle
  seed *text*; a ref-handle seed (another `figure`, a `cad` cross-section) is
  only listed as a titled reference. Render richer per-kind seed content (e.g.
  a figure's SVG source, a cad analysis) so "here's another view + a cross
  section" fully lands in the turn.
- **`~~Self-directed drawer (slice-5 upgrade)~~` — SHIPPED (2026-07-16, main
  `6585223d`).** Design-of-record `docs/proposals/diagram-propose-loop.md`
  landed; the precursor tick was upgraded in place (supersede, not layer). The
  drawer now FINDS+BINDS its own sources via a three-layer context — **L1**
  owning-draft collapsed outline + **L2** entity-seeded fisheye of the
  instruction's paragraphs (`diagram/doc_context.py`, wired into `run_turn` for
  figures) + **L3** a tool-using agentic turn (`diagram/agent.py`
  `build_agentic_claude_fn`, self-gated on `PRECIS_MCP_CONFIG` /
  `PRECIS_DIAGRAM_AGENTIC`). Seeds kept as optional hints. Motivated by the
  deck-hook incident (a figure drawn wrong from a title alone). Finder: Opus
  session. **Remaining follow-ups (deferrals from that build):**
  - **mermaid L1/L2 auto-context** — `feature`, Owner: `store/_draft_ops.py` +
    `diagram/doc_context.py`. Figures get L1/L2 for free via the `has-figure`
    reverse resolver (`figure_owning_draft`); mermaid has no `mermaid`-owning-draft
    resolver, so a mermaid tick runs seeds + L3 agentic tools only. Add the
    reverse resolver + route `document_context_for` for mermaid.
  - **L2 semantic leg** — `feature`, Owner: `diagram/doc_context.py`.
    `pick_paragraphs` matches deterministically (keywords + verbatim over the
    owning draft's own chunks, no embedder). Add the semantic retrieval leg
    (embed the instruction entities, rank the draft's chunks) so L2 catches
    paraphrase, not just literal term hits. Today the semantic reach is
    delegated to L3's agentic search.
  - **MCP `vocab`/`notes`/`element` plumbing on `edit`/`link`** — `bug`, Owner:
    `handlers/figure.py` (+ MCP tool schemas). The exposed `edit` tool only
    carries `text=` (strips `vocab=`/`notes=`/`viewbox=`) and `link` lacks
    `element=`, so an agent can't update a figure's vocab/notes or set an
    element→chunk binding over MCP — the gap that blocked a live vocab update.
    Expose the fields on the tool surface. Finder: Opus session.
- **`wip/backlog-docs` branch (primary repo)** — `polish`, Owner: git. The
  2026-07-15 main-fix preserved one local-only commit (`e5643873 docs(backlog)`)
  on branch `wip/backlog-docs` in the primary checkout. Ship it or drop it.

## 🔵 Turn-as-job routing + context DSL — WIP design (2026-07-07)

- **Status**: `deferred` (design captured, not sliced) —
  **Severity**: `feature` — **Owner**: `handlers/job.py` +
  `workers/dispatch.py` + `utils/handle_registry.py` +
  `utils/prompt/` (assembler) + a scheduler affinity layer; router
  touches ADR 0046 / `utils/claude_agent.py`.
- **Design of record**:
  [`docs/proposals/turn-routing-and-context-dsl.md`](docs/proposals/turn-routing-and-context-dsl.md).
  Every turn is a `kind='job'`; Part 0 = thread persona + cache-ordering
  gradient + affinity scheduling; Part 1 = delegate-on-confidence
  routing (Opus drives, assigns helpers); Part 2 = the stateful context
  DSL (ADR 0036 handles + fidelity ladder, receipt-default collapse).
  Taxonomy: Followup / Call / Spawn. First slice = persist turn-as-job +
  shadow router.

## 🟠 Planner "new writing task" wizard — don't auto-dispatch on create (2026-07-07)

- **Status**: `done` — **Severity**: `feature` — **Owner**:
  `precis_web/routes/tasks.py` (create-root form + start route) + `dashboard.html.j2`.
  **Done**: The new-root form is now a wizard with description, doc-type select, and
  a "Start now" checkbox. `start` unset creates a parked `level:strategic` root with
  no `LLM:`/executor tag; `start=on` stamps `LLM:opus` and seeds `meta.workspace`.
  The `▶ start` button seeds workspace + `LLM:opus` for parked roots.
  **Test**: `tests/precis_web/test_tasks.py`.
- **Why**: today a strategic root is born with `LLM:opus` +
  `level:strategic` already attached, so `dispatch` mints a `plan_tick`
  the instant it exists and the planner fans the whole doc into
  section-todos — each of which auto-gets `LLM:opus` and dispatches its
  own tick. Reto watched this run away three times in one session on a
  "Suitcase Design" paper (roots 52247, 52306) and had to SQL-kill the
  subtrees. The **stop/start buttons shipped 339b77f4** make the kill
  one click, but the root cause is *creation auto-starts the planner*.
- **Spec (agreed this session)**: replace the bare new-root form
  (`dashboard.html.j2:185-203`) with an old-timey **wizard** that
  *collects intent before outputting*:
  - a **description** textbox ("what are we doing / what's in the doc"),
  - a **doc-type** select (paper / draft / pres / cfp / …),
  - a **"Start planning now"** checkbox, **default OFF**.
  `POST /tasks/roots` gains `doc_type` + `start`. **OFF** → create the
  root **with no `LLM:` tag** (stash description + `meta.doc_type`); it
  sits parked, invisible to `dispatch` (absence of the planner tag *is*
  the gate — no dispatch change needed). **ON** → stamp `LLM:opus` as
  today. The **▶ start** button on a parked root then stamps `LLM:opus`
  (+ seeds the chosen doc-type workspace) to begin.
- **Open design point** (Reto leaned "every planner todo gated" earlier,
  then reframed to the wizard): once a root is started, do its
  section-children flow automatically (recommended — else ▶ per
  section), or stay individually gated? The shipped ⏹/▶ already give
  per-subtree control on demand, which argues for root-only gating.
- **Test**: a `POST /tasks/roots` with `start` unset creates a todo
  carrying **no** `LLM:`/executor tag (dispatch-invisible); with
  `start=on` it carries `LLM:opus`.

---

## 🟡 Unified item view (`/items`) — one DRY cross-kind list/search

**Status**: slices 1–3a shipped + deployed; rest of slice 3 + slice 4 open
· **Severity**: feature · **Owner**: `precis_web/routes/items.py`,
`precis_web/item_view.py`, `handlers`/`store` search surface
· **Design**: [`docs/proposals/unified-item-view.md`](docs/proposals/unified-item-view.md)

One surface where the human's filter == the LLM's retrieval scope (a
tailored view is a context set). Author/source is a `kind` facet, not a
separate page.

- **Slice 1 — reading-intent flags → shipped + deployed** (`94a5dcc1`).
  `read-later`/`must-read`/`skim` toggle buttons (kind-agnostic
  `POST /flags/{kind}/{ref_id}` + `_flag_buttons.html.j2` + batched
  `Store.ref_tag_values`), first on `/papers-needed`; ride through ingest.
- **Slice 2 — cross-kind search primitive → shipped + deployed** (`f1139ef7`).
  `Store.search_chunks_across_kinds` (RRF lexical+semantic over `refs.kind =
  ANY(...)`, per-ref best chunk, `created_at` window, relevance|recency);
  `search` verb gained `kinds`/`sort`/`since`/`until` → `_dispatch_source_search`.
- **Slice 3a — `/items` page + presenter seed → shipped + deployed**
  (`efce60df`…`f2c027b3`): read-only cross-kind search page; per-item tag
  chips + grouped `[kind][state]` markers; New dropdown; kind chips
  (All/None, cookie-remembered); tag autocomplete→chips filter
  (`GET /items/tags/suggest`); "recently added" default landing; stub filter
  (papers-to-get); UoL/Scholar/DOI find-links (shared `precis_web/paper_links.py`).
  `ItemPresenter` is a plain class with a generic default (`open_url`/`state`/
  `links`/`preview`), not yet the abstract contract.
- **Rest of slice 3 — open.** Promote `ItemPresenter` to the full contract
  (`preview(query)->text|image`, `hover_preview`, `thumbnail`, `actions`) and
  to `@abstractmethod` once every kind adopts (check-time totality); result
  pagination (currently capped at 30, no paging); author/source facet +
  folders + thumbnails/hover for visual kinds; retire `/drive` /
  `/papers-needed` / `/papers/triage` / `/refs` / `/tags/refs` into `/items`
  filters.
- **Coupled — kind-taxonomy audit — open.** Reconcile `role`/`corpus_role`
  drift (datasheet `evidence`+`stream`; pres `corpus`+`none`), collapse
  near-dup kinds (perplexity-*/websearch/web/wikipedia; calc/math/oracle),
  rewrite `precis-*-help` skills. No-legacy-alias license (a fresh LLM
  re-reads skills each session): interface free to change, data isn't.
- **Slice 4 — "write a document from this view" — open.** A tailored filter
  is a serialized query → mint an authoring job scoped to exactly those refs.
- **Verification residual.** The `/items` filter-bar JS (Alpine
  tag-autocomplete + chip add/remove) is backend-tested but not visually
  verified — eyeball the live page.

## 🟢 Draft inline editor (click-to-edit prose, no LLM)

**Status**: shipped + deployed (core complete; only optional extensions + a
verification residual remain, below) · **Severity**: feature · **Owner**:
`precis_web/routes/drafts.py`, `static/`, `handlers/draft.py`
· **Design**: [`docs/design/draft-inline-editor.md`](docs/design/draft-inline-editor.md)

Direct human editing of `draft` prose from the reader — click a paragraph,
edit raw text, save on click-out, `+`/delete paragraphs — bypassing the LLM
change-request path. **Model B** (a box per chunk, wired caret handoff,
contained edits; the full rationale + refs-as-decorated-text, spellcheck,
split/merge, and the vendored-PM-bundle spike are in the design doc).

- **Slice 1 — validation core → shipped.** `DraftHandler._newly_dangling`
  (old-vs-new dead-ref diff) + extracted `_dangling_chunk_tokens` /
  `_dangling_finding_tokens` + advisory `_dangling_edit_hint` wired into the
  MCP/CLI edit path (previously the edit path gave *no* dangling feedback).
  Test: `test_edit_flags_newly_introduced_dangling_ref`.
- **Slice 2a — text-edit MVP → shipped + deployed.** `POST /drafts/{ident}/text`
  (hard `_newly_dangling` 422 gate → `edit` verb → soft-warning tail); per-block
  ✎ editor (plain textarea, saves on blur/⌘↵, Esc cancels) on prose kinds only
  (`_EDITABLE_KINDS`); `base_sha` optimistic concurrency (sha computed in
  `_build_rows`, no dataclass change); on success a `draft:edited` event drives
  `draftDoc.rehydrateOne` to refresh the block in place + a toast. Tests:
  `test_newly_dangling_returns_only_new_breakage`; live-verified on melchior
  (markup served + endpoint JSON contract). **No ProseMirror yet.**
  Browser-confirmed working by Reto (edits save persistently). Two `x-data`
  quoting bugs found + fixed post-deploy (the editor's `| tojson` double-quotes,
  and a **pre-existing** wordcount-badge one) — both now have parse-level
  regression tests (`test_inline_editor_xdata_is_single_quoted`,
  `test_wordcount_badge_xdata_is_attribute_safe`). **Lesson:** verify the
  rendered attribute *value* (parse it), not substring presence.
- **Slice 2b-i — inline add / delete blocks → shipped + deployed.**
  `POST /drafts/{ident}/block` (empty paragraph after an anchor via
  `store.add_chunks`, since `put` rejects empty text) + `POST
  /drafts/{ident}/block/{handle}/delete` (`delete` verb, `cascade=1` for a
  heading's subtree, kind-aware confirm). Client: ＋¶ / 🗑 hover controls on
  editable blocks; a new block auto-opens its editor (`__draftAutoEdit` flag
  consumed by the row's `init()`). Test:
  `test_add_empty_block_inserts_paragraph_after_anchor`; live add+delete
  round-trip verified net-zero on dream-review (21→22→21). *Scoped to editable
  prose kinds — figures/tables keep their own controls.*
- **Slice 2b-ii — ProseMirror editor + live squiggle → shipped + deployed.**
  Vendored `static/prosemirror.bundle.mjs` (206 KB / 63 KB gz — only the modules
  used: state/view/model/keymap/commands/history/TextSelection; **not** stock
  prosemirror-markdown — it escapes our ref brackets). Minimal schema
  (`doc > block > text|hard_break`, identity round-trip, headless-verified);
  `POST /drafts/{ident}/validate-refs` (reuses the dangling-token helpers) drives
  a debounced **red wavy squiggle** (`.ref-bad`) on unresolved refs as you type —
  the live face of the save-time gate. Editor **replaces** the rendered text in
  place (rendered view is `x-show="!editing"`, editor a sibling), caret
  auto-placed at end, raw toggle retired. Mirrors into the hidden textarea so the
  save/validate flow is unchanged, and **falls back to that textarea** if the PM
  module fails to load/mount (can't regress). Editable prose kinds only. Browser-
  QA'd by Reto. *Verification gap:* no headless-browser test — bundle load,
  schema round-trip, endpoint, `.mjs` MIME, and syntax are checked, but the
  contenteditable/decoration rendering rides on the fallback + Reto's eyes.
- **Slice 2b-iii — editor-feel (caret flow + split/merge) → shipped + deployed.**
  **Enter** splits at the caret (`/block/{h}/split`: current keeps `before` + its
  handle, new chunk gets `after`, opens caret-at-start); **Shift-Enter** = soft
  break. **Arrow up/down past the top/bottom line** hands off to the neighbouring
  editable block (`draft:goto` → `_neighbour` skips figures/tables → `openEditor`
  at end/start; a registry `__dEditors` + a retry loop make it robust to
  live/placeholder/recycled rows). **Backspace at start** merges into the previous
  block (`/block/{h}/merge-prev`: client sends live text so unsaved keystrokes
  survive; empty block → just delete + go to prev end; caret at the join offset —
  doc pos is `1+offset`; no-ops rather than folding a heading away). Split-point
  math headless-verified (before+after reconstructs across 61 caret positions);
  tests `test_split_keeps_handle_and_inserts_tail_after`,
  `test_merge_prev_joins_text_and_deletes_block`; endpoints live-verified
  net-zero on dream-review. Same headless-browser verification gap as 2b-ii.
- **Slice 3 — polish → shipped + deployed.** (1) **`[`-autocomplete**:
  `GET /drafts/{ident}/ref-search` title-searches held papers
  (`find_papers_by_title` + `fetch_refs_by_ids`) → citation tokens; a PM
  dropdown (arrow/enter/tab, coordinates with the split/leave keymap via an
  `ac.active` gate) inserts `[§slug]`. (2) **Reveal-on-cursor**: a selection-aware
  `chipPlugin` styles ref tokens (`REF_TOKEN` regex) as chips, showing raw only
  when the caret is inside — the in-editor complement to the reader's already-
  pretty non-editing view. (3) **Removed the dead `tailwind.js`** Play CDN (the
  static build is confirmed good). All wrapped so a failure degrades gracefully
  (autocomplete `try`-guarded; chips are display-only). Live-verified:
  `ref-search?q=attention` → real papers; `tailwind.js` 404, `tailwind.css` 200.
  - *The editor is complete.* The deferred extensions and the verification
    residual are broken out as their own backlog entries below.
- **Draft editor — deferred extensions** → backlog (optional, none block use):
  - **`[`-autocomplete over non-paper kinds** (chunks / findings), not just held
    papers — extend `GET /drafts/{ident}/ref-search` + the picker's result set.
  - **Resolved-title chips** — reveal-on-cursor shows the raw token today; a small
    resolve endpoint would let the chip show the target's title/section instead.
  - **Structured-block creation from the editor** — a slash-menu to insert a new
    table / figure / code block inline (today those use the existing buttons).
  - **Per-draft language selector** for browser spellcheck (defaults to OS lang).
- **Headless-browser verification in CI** → backlog (testing infra, high-value).
  The interactive editor + virtual-scroller JS has **no gate coverage**, and
  several browser-only bugs reached prod this session — the `x-data` `| tojson`
  quoting, Safari `group-hover`, `forceRefresh` early-return, and the
  programmatic-open **focus** bug. An **ad-hoc Playwright-over-SSH-tunnel harness**
  (2026-07-05) both *found* and *proved* the focus bug (system Chrome via
  `channel:'chrome'`, tunnel to `melchior:8000`, `page.evaluate` probes of
  `window.__dEditors`). Wire a slim version into `scripts/ship`/the gate: boot the
  web app on the test DB with a seeded draft, load a page, assert a **clean
  console** + a couple of core interactions (open editor → focused; arrow → the
  neighbour opens focused). Closes the recurring "ship blind, Reto QAs" loop that
  cost several round-trips this session.

---

## 🔵 Retire the `equation` chunk kind → math as `$…$` / `$$…$$` in prose

**Status**: decided · **Severity**: feature (simplification) · **Owner**:
`draftimport/`, ingest/paper pipeline, `precis_web/routes/{papers,drafts}.py`,
`export/latex.py`, a forward migration

**North star.** No dedicated `equation` kind. Math is LaTeX *inside* prose —
inline `$…$`, display `$$…$$` — KaTeX-rendered on read, edited as raw source.
It behaves like every other block; no special reader/editor/export branch, no
`needs-math-review` quarantine.

**Scope reality (verified 2026-07-05, `precis_prod`).** ~54,920 live `equation`
chunks, and they are **overwhelmingly papers, not drafts**:
`paper = 54,642 · draft = 278`. So the earlier draft-scoped project prompt
covers the *minority* — **papers are the real target** and have different
mechanics. The two fronts:

- **Drafts (278) — sorted.** Mutable chunks from the LaTeX importer
  (`draftimport/tex.py`, `_MATH_ENVS`, `needs-math-review`); draft reader; bodies
  are bare LaTeX + `\label{eq:…}`. Plan = the project prompt held by Reto
  (2026-07-05): stop minting `equation` in the importer, migrate to `$$…$$`
  paragraphs, drop the reader/editor special-casing.
- **Papers (54,642) — the bulk, needs its own handling.** Different in three
  ways: (1) **append-only body chunks** — migration is DELETE+INSERT (which
  re-runs the embed/summary/keyword cascade at 55k scale), not in-place edit;
  (2) produced by the **Marker/PDF ingest** pipeline, *not* the LaTeX importer —
  the "stop producing `equation`" fix lives there; (3) rendered by the **two-pane
  paper reader** (`routes/papers.py`), not the draft reader. Paper equations carry
  source numbering like `\tag{A.5}`, e.g.
  `H^{+}(aq) + e^{-} \leftrightarrow \frac{1}{2} H_2, \tag{A.5}`.

**Shared work (both fronts).** A KaTeX-safe body normalizer (strip
`\label`/`\tag`, unwrap `\begin{equation}`, map `align`→`aligned`, etc. — a pure
tested `body → "$$…$$"` fn with a gold set); the numbering / `\label` / `\ref`
decision (drop, auto-number, or map to `[¶handle]`); LaTeX export of `$$…$$`
(and inline `$…$`); and `strip_markers`/card-combined handling so `$$` doesn't
break embeddings. Forward-only migration, dry-run → `--commit`, reversible until
browser-confirmed (use the Playwright-over-tunnel harness above). **Interim
alternative** if the full retire isn't scheduled: just make `equation` *render*
(wrap bodies in `$$` for KaTeX in both readers) — first-class display without the
migration.

---

## 🟢 Dark-factory build/deploy workstream

**Status**: in progress · **Severity**: feature · **Owner**: `scripts/`,
`.claude/commands/`, `CLAUDE.md`

North star: `claude -w <feature>` → describe the spec → `/go` → the change
is implemented → gated → merged → deployed, with the LLM asked only "OK?" or
handed a genuinely broken test. Every mechanical step is a script (token-cheap,
reproducible); the model spends tokens on judgment, not CI/CD plumbing.

- **`scripts/deploy` + `/go`** → **shipped this workstream.** `scripts/deploy`
  is the non-interactive ansible-redeploy backbone (twin of `scripts/ship`,
  no LLM in the loop); `/go` = `scripts/ship` then `scripts/deploy` on green
  (the one-keystroke ship+deploy). `/endsession` stays deploy-free.
- **Token-lean session boot** → **partly done.** `## Other live affordances`
  in CLAUDE.md compressed to a one-line-per-kind index (detail already in the
  `precis-*-help` skills) — ~33% fewer boot bytes. Ties into the existing
  cold-start work (`docs/design/mcp-cold-start-token-budget.md`,
  `PRECIS_STARTUP_SKILLS`). Next: apply the same discipline to the
  `~/work/cluster` CLAUDE.md; measure boot token delta.
- **`/whatneedsdoing`** → **shipped this workstream.** One triage view over the
  **two work substrates** — *repo dev work* (`OPEN-ITEMS.md` + open gripes,
  `get(kind='gripe', id='/open')`; fixed by editing this repo → `/go`) and the
  *prod factory queue* (open/doable todos, `search(kind='todo', view=…)`; the
  loop runs these on the cluster) — plus a latent-bug source: LLM-confusion
  mined from prod `plan_tick` transcripts (feeds new gripes into substrate 1).
  It keeps the substrates separate rather than flattening them, flags which
  todos are autonomous vs stalled, and names the *bridge* — a prod todo failing
  because of a repo bug.
- **Backlog groomer (close the loop) — gripe slice shipped; OPEN-ITEMS half open.**
  The dark-factory move: promote declared repo dev work into the acting queue so
  `dispatch` builds it, bridging repo dev work *into* the prod factory queue.
  - **Gripe side → shipped.** `workers/backlog_groom.py` (`run_backlog_groom_pass`)
    mints one `kind='todo'` per open gripe carrying
    `meta.executor='claude_inproc'` + `meta.job_type='fix_gripe'` +
    `meta.params={'gripe_id': N}`, hung under a `level:strategic` groomer root
    (find-or-create, so children aren't nursery orphans). Deduped on
    `meta.params.gripe_id` (no re-mint even after the fix todo is done);
    `no-groom` open tag is the human opt-out; cadence-throttled
    (`backlog_groom:last_run`, `PRECIS_BACKLOG_GROOM_REFRESH_HOURS` default 6) +
    single-runner `pg_try_advisory_xact_lock`. Registered **default-OFF** in
    `cli/worker.py` (`--only backlog_groom` / `PRECIS_BACKLOG_GROOM_ENABLED=1`) —
    enabling it starts handing repo bugs to `claude_inproc`, a deliberate flip
    like the classifier. Tests: `tests/test_backlog_groom.py` (incl. the
    end-to-end hand-off — the groomed todo is a valid `dispatch` candidate that
    mints a `fix_gripe` job).
  - **OPEN-ITEMS half → open (residual, filed 2026-07-06).** Not groomed, for two
    concrete reasons: (1) `OPEN-ITEMS.md` lives at the repo root and is **not**
    packaged into the installed wheel, so a deployed worker can't read it — needs
    a packaged/DB-backed source of the backlog first; and (2) there is **no**
    `build_feature` job_type for a free-text feature item to hand off to
    (`fix_gripe` is gripe-specific) — needs a build executor. Both are
    prerequisites before a feature item can become a dispatchable todo.
  - **Activation (ops, when ready).** Flip `PRECIS_BACKLOG_GROOM_ENABLED=1` on a
    system-profile worker to start draining open gripes into `fix_gripe` todos;
    watch the first pass's mint count + the fixer's throughput before widening.
  Pairs with `/checklogs` + cheap-model tiering.
- **Post-ship residual follow-through** → **shipped this workstream.** `/go`
  and `/endsession` now end with a tiered follow-through step: after a green
  ship, harvest the latent bugs the session parked — gated to **Opus-4.7+
  finders** (this session or an opus reviewer memory; nursery-SQL / haiku
  findings are filed, not chased) — persist them durably (so they survive the
  harness's self-compaction), fix the in-reach ones in their own worktree→ship
  cycles now, and file the investigations as todos/gripes. The "file the rest"
  half feeds the Backlog groomer above; the "fix now" half is the in-session
  interim until that groomer lands.
- **`/testfeature <prompt>`** → open. Agent loop that exercises the precis MCP
  surface (`scripts/exercise-mcp` is a seed), finds bugs, applies fixes, then
  `/go`. Bounded by a turn/cost cap.
- **`/checklogs`** → open. Read the recent LLM-error surface (prod `agentlog` +
  `alert` + failed `kind='job'` + error `ref_events`; local `.claude` logs +
  `/var/log/precis-worker-agent.log`), cluster the top-N recurring failures,
  fix root cause, `/go`.
- **Cheap-model tiering** → open. Route mechanical LLM work (`llm_summarize`,
  triage children, CI-fix escalation) to a small 4B–14B model; reserve Opus for
  build/planner/reviewer judgment.
- **Out-of-band DB-liveness monitor** → open (ops-observability). The
  2026-07-05 ~03:00→11:00 prod outage (caspar's Postgres/pgbouncer host
  degraded from ~22:00, flapped, then died) ran **~8h completely unalerted**:
  every alerting path we have (`nursery` → `kind='alert'`, `precis-heartbeat`)
  is **DB-backed**, so when the DB itself dies the alerter can't fire — and
  can't even write the alert saying it's down. Blind spot: total DB death is
  invisible to the system's own monitoring. Needs an external liveness check
  that does **not** depend on `precis_prod` being up — e.g. a tiny watcher on a
  different host (or the fixer host / a cron on the laptop) that `SELECT 1`s the
  pgbouncer endpoint every N min and pushes to Discord/PushNotification on
  failure. The precursor was visible ~5h early (per-host `worker_logs` volume
  halving from 22:00), so a degradation trend-alarm is a cheap second signal.
  Pairs with `/checklogs`.
- **Widen `scripts/ship` auto-fix surface** → open (polish). Auto-fix + amend
  anything the gate can resolve without judgment (import sort, trivial mypy
  stubs); only real logic failures reach the model.

Deferred (revisit later): **holdout scenarios** (StrongDM-style anti-overfit
eval outside the repo — not needed while Opus shows no test-gaming; ADR 0047
gold sets are the seed); **digital-twin fidelity** (richer stubs so
green-in-twin/red-in-prod gaps close — the current `FakeStore`/`MockEmbedder`/
`PRECIS_CLAUDE_BIN` twins are good enough for now); **auto-deploy as a daemon**
(vs `/go`-chained — only if chaining proves insufficient).

## 🟠 Worker liveness + observability (2026-07-05)

**Status**: slice 1 shipped · **Severity**: critical (was a silent 1.5-day
outage) · **Owner**: `workers/nursery.py`, `cli/worker.py`, `alerts.py`,
cluster repo · **Origin**: the mofs-for-electrodes plan_tick stall — melchior's
agent worker (only host running `plan_tick`) was jetsam-culled ~50-200×/day
under llama.cpp wired-RAM pressure, orphaning every in-flight tick, invisible
for 1.5 days because nothing watched daemon health.

**Slice 1 — observability → shipped + deployed (81a197c7).** Boot-event row at
`cli/worker.run`; nursery `worker-restart` + `dead-worker` critical detectors;
`raise_alert` → `(ref_id, is_new)`; one-shot `notify_critical_alert` → Discord
webhook `PRECIS_OPS_ALERT_WEBHOOK`. Tests in `test_nursery.py` / `test_alerts.py`.

### Residuals — docx / EndNote export session (filed 2026-07-05, Opus-authored)

Shipped this session: docx paper theme (black Times New Roman, 1-in margins);
docx handle-citation resolution fix (`[pa<id>]`/`[pt<id>]`/`[fi<id>]` — imported
drafts were exporting with **no** References section); and native EndNote CWYW
export (`precis/export/endnote.py`, `citations=endnote` / `?citations=endnote`)
emitting `ADDIN EN.CITE` + `EN.REFLIST` + `EN.*` doc-vars with the full record
as a traveling library. Format reverse-engineered from a real EndNote sample and
independently confirmed by web research (Journal Article=17, DOI in
`electronic-resource-num`, one field/cite, `EN.REFLIST` is a marker, traveling
library reformats with no library open). **Also shipped** (f1b6f82f): `[pc<id>]`
chunk citations embed that chunk's exact text as the record's `<research-notes>`
(traveling provenance, per-cite-site not per-paper) via `Store.chunk_text_by_id`
— on nanobuds 54/93 cites carry their source passage.

- **EndNote round-trip is validation-pending (not a code bug).** The CWYW format
  is undocumented/version-sensitive; correctness can only be confirmed by opening
  the export in real Word+EndNote and running "Update Citations and Bibliography".
  Reto has Word+EndNote and is testing; a sample was generated straight off prod
  via the `PRECIS_DATABASE_URL` secret (rewrite `host.docker.internal`→`127.0.0.1`)
  + `export_docx(citations='endnote')`. If it doesn't reformat cleanly, likely
  culprits: per-document output-style storage (publicly undocumented — recipient
  may need to pick the style once in EndNote's Word toolbar), or `db-id` collision
  with an open library.
- **`EN.Layout` style is hardcoded to `"Annotated"`** (`endnote.install_document_vars`).
  Fine as a default (recipient can change it), but a numbered/IEEE default might
  suit a references-heavy manuscript better. Make it a param if requested.
- **docx `[dc<id>]` cross-refs render as plain surface text, not Word
  cross-reference fields** (the LaTeX exporter emits `\cref`). Pre-existing
  fidelity gap, low priority — bare `[dc]` with no surface text still renders
  nothing. A real Word `REF`/bookmark cross-ref field would close it.
- **Cited-passage embedding — SHIPPED (f1b6f82f), round-trip still unverified.**
  `[pc<id>]` cites now carry the chunk text as `<research-notes>`. Caveat holds:
  EndNote **drops** Abstract/Notes/Research-Notes when a traveling library is
  imported into a real library, so the passage is visible in the field data +
  survives a reformat-in-place, but does **not** persist into the recipient's
  library. If persistence is wanted, retry with a `<custom1>` field (may survive)
  — needs the same Word+EndNote round-trip test. `pa<id>` ref-level cites carry no
  passage (correct — no chunk). Cap `_NOTE_MAX_CHARS=4000` per note.

### Residuals (filed 2026-07-05; all Opus-authored this session — harvest-eligible)

- **Activate the page (ops, in-reach).** The critical push is dark until
  `PRECIS_OPS_ALERT_WEBHOOK` is set on the system-profile workers (ansible env,
  cluster repo). Until then worker-restart / dead-worker alerts only land in
  `/alerts` — visible but not proactive. Set it to actually get paged.
- **#2 Tier B — lease as the single job-substrate liveness authority.** Today
  two clocks: `claim_executor_jobs` re-claims only `STATUS:queued` (lease-keyed),
  so a crashed `STATUS:running` job is unreachable and only the sweeper's
  independent `PRECIS_STUCK_JOB_HOURS` clock rescues it (fail→bubble→retry). Let
  the reclaim path take over a `running` job whose lease has expired
  (requeue-from-checkpoint: `meta.coordinator_state` for the coordinator, fresh
  tick for plan_tick), then **retire the sweeper's hours clock** — lease becomes
  sole authority. Needs a per-job attempt cap (the sweeper's terminal-fail is
  today's backstop). Riskier; gated behind slice 1 (now shipped) so misbehaviour
  pages. Owner: `executors/_common.py`, `sweeper.py`, `executors/coordinator.py`.
- **#3 short — de-SPOF the agent worker.** `plan_tick` jobs are NOT node-pinned
  (`claim_executor_jobs` node gate is null for `claude_inproc`); the melchior-only
  confinement is purely operational (hermes `~/.claude` OAuth + `PRECIS_MCP_CONFIG`
  live only there). Provision a **second agent host** (caspar/balthazar) with the
  OAuth state + an agent-profile daemon → one worker dying no longer stalls all
  planning. Ops/ansible, no code. Highest-value #3 move.
- **#3 medium — co-location relief.** Get the ~73 G `mlock`'d llama.cpp weight off
  the agent host (or drop `--mlock`/cap it) so jetsam stops targeting the worker.
  `ProcessType=Interactive` (shipped on cluster `master` 7e1258f) is a mitigation,
  not immunity. Ops.
- **#3 long — sandbox substrate.** The `sandbox_run`/`claude_docker` substrate
  (ADR 0048, `docs/proposals/sandbox-run-substrate.md`) runs ticks in isolated
  containers, immune to host memory pressure and naturally multi-host — subsumes
  both the SPOF and co-location. Big lift; the durable north star.
- **Config-drift guard (cluster repo).** The `ProcessType` fix regressed once
  because it sat on an unmerged branch while deploys render from `master`. Add a
  deploy assert that deployed launchd plists match the rendered templates (analogue
  of the existing venv-commit convergence assert). Owner: `redeploy-precis.yml`.
- **Convergence assert races the autonomous fixer → FIXED** (cluster `master`
  `3ff4fc2`, 2026-07-08). The install, the pre-flight gate, and the convergence
  assert each re-sampled `git ls-remote origin main` independently, so a commit
  landing mid-deploy (the hephaestus fixer, a sibling `/go`) left the venvs on the
  sha they installed while the assert compared against a NEWER HEAD → spurious
  "DEPLOY DID NOT CONVERGE" on a uniform cluster (hit 2–3× per deploy while the
  fixer was active; a quiet-window re-run converged). Fix: `redeploy-precis.yml`
  step 0 resolves the ref to ONE commit via a single `run_once` `git ls-remote`,
  broadcasts it to all hosts, and pins the three `precis_*_git_ref` install vars +
  a `precis_target_sha` compare target to it; install (`@<sha>`), pre-flight, and
  assert all use the frozen sha, so a `main` that advances under an in-flight deploy
  can't false-fail it. `-e precis_worker_git_ref=<branch>` still wins. Validated
  live: deployed cleanly first-try *while* the fixer was shipping (`main` moved
  `6c9c8a01`→`aa74b0d1` mid-run), pinned + converged on all 4 hosts, `failed=0` —
  the exact condition that needed a manual re-run twice before, now green in one
  pass. Retires the old workaround ("confirm your sha is an ancestor of
  origin/main and re-run"). Owner: `redeploy-precis.yml` (cluster repo).

## 🟢 Chunk-tag classifier (ADR 0047) — remaining work

**Status**: open · **Severity**: feature · **Owner**:
`src/precis/workers/classify.py`, `src/precis/data/axes/`, cluster env

The `junk`→`ROLE3` cascade is **shipped + deployed + validated** (worker
pass ran green on melchior, `claimed=16 ok=16 failed=0`; 1,521 `ROLE3`
tags on prod from the bounded backfill). Design:
`docs/design/chunk-classifier-cascade.md`; numbers: `scripts/classify/
EVAL_RESULTS.md`. What's left:

- **Enable continuous corpus tagging** — the worker pass is deployed
  **default-OFF**. Flip `PRECIS_CLASSIFY_ENABLED=1` on the system-worker
  daemon (melchior, or cluster-wide) to drain the remaining ~1.29M chunks
  on the free `summarizer` model. Deliberate large backfill; watch load.
- **Tier-2 escalation (optional)** — set
  `PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5` to re-judge `own`
  chunks and push own-claim precision past 91%. Was HTTP-429 blocked during
  dev (proxy Anthropic quota); retry when free. Cost tradeoff, ~$200-400 on
  the residual vs ~$1.3-2.6k all-haiku.
- **Ref-axis production runner (`classify-papers`)** — not built. Only
  `material` (93%) and `transport` (97%) clear the gate on the free model;
  `domain`/`studytype`/`property` need a stronger model. Walk `paper` refs,
  apply `applies_when` gates, write ref tags + `meta.processing.<axis>`.
- **Better table detection (polish)** — the free Tier-0 `numeric_ratio`
  heuristic catches only 0.1% (tables aren't digit-dense; labels+spaces).
  Tables currently fall to the LLM (handled, but not free). A pipe/tab/
  repeated-token heuristic would recover the ~free furniture drop.

## 🔵 `serverInfo.title` not set

**Status**: blocked on upstream `FastMCP`
**Severity**: polish
**Owner**: `src/precis/server.py:129`
**Test**: `tests/test_server_init.py::test_serverinfo_carries_title`

MCP spec 2025-06-18 §A1 recommends a human-facing
`serverInfo.title` alongside the machine name. Today's
`FastMCP("precis-mcp", instructions=_INSTRUCTIONS)` constructor
takes no `title=` kwarg — we get `serverInfo.name = "precis-mcp"`
and no `title` field. One-line fix once `FastMCP` accepts
`title="Precis"`. Track upstream:

- https://github.com/modelcontextprotocol/python-sdk/issues — file
  the request when the next mcp-critic pass surfaces it again.

## 🟠 LLM-confusion bugs mined from prod plan_tick transcripts (2026-07-03)

Mined 48h of `kind='job'` `meta.transcript` on `precis_prod`: **702**
`[error:*]` tool-call errors, 544 `BadInput`. Two clusters. The **tex
workspace-authoring** cluster (the top ~450 errors) is **fixed on this
branch** (`worktree-serverconfusion`): `put(mode='find-replace')` now
redirects to `edit`; the "unknown view" error suggests the `--` slug form
when an extensionless path collapsed into a view; the slash-in-`name=`
error tells the LLM to pass the bare slug; `precis-tex-help` now documents
the workspace `name=` form + the load-bearing extension. Remaining:

- **DONE — extensionless slash-path collapse (root fix).** `_parse_file_id`
  now takes the handler's `_SUPPORTED_VIEWS`: when a slash-path's tail isn't a
  real view it's encoded to its `--` slug (`tex/graphene` → `tex--graphene`,
  `projects/x/tex/graphene` → `projects--x--tex--graphene`) via
  `file_slug_from_path`, so it addresses the file instead of splitting into a
  bogus view. `slug/raw`,`slug/toc` still resolve as views; an unsupported
  view via the explicit `view=` kwarg still raises `Unsupported`. Regression
  tests added (tex/plaintext/markdown).

- **DONE (A1) — bare-numeric paper id ref_id fallback.** `resolve_live_slug_ref`
  now resolves a bare all-digits id as the kind's `ref_id` for slug-addressed
  kinds (paper/draft/tex/…) and emits a `warn` admonishing the agent to use the
  `pa<id>` handle and never write bare numbers into cited text. (The intended
  addressing already existed: `pa1876` is the ADR 0036 handle; `get(id='pa1876')`
  works with no `kind=`; `kind='pa'` is an alias.)

- **DONE (B) — merged-duplicate handles now redirect (universally).**
  `reconcile` already stamps `meta.superseded_by` on the loser;
  `Store.follow_supersede` + `resolve_handle` + `parse_link_target` transparently
  follow it to the live survivor (chains capped/cycle-guarded). The redirect
  hint now fires from the **store layer**: `Hub.__post_init__` wires
  `store.hint_bus = hub.hints`, so `resolve_handle` emits the "please use the new
  handle" nudge on **every** path (get, all `link=` incl. `apply_link_ops`,
  `exclude=`, citation `source_handle`) with no per-callsite `hub` threading. The
  A1 admonish moved to the same bus. Residuals cleared: `apply_link_ops` covered
  (via the store bus); citation `source_handle`'s paper-existence check now
  follows supersede too.

- **P0 operational: `nanotrans_auto` planner spin — root cause found.** One
  plain-tex-workspace project re-minted **47 `plan_tick` ticks in 48h** since
  2026-07-01, creating orphaned duplicate `\section{…}` refs (`workspace=∅`)
  every tick while `latexmk` stayed broken. **Root cause:** every tick exits
  `STATUS:succeeded` with **no** `resume_reason` / `resume_streak` — the
  coroutine "succeeds" (verdict: continue) each tick but never converges
  because tex authoring kept failing. The resume-streak cap
  (`meta.plan_tick_resume_streak`, default 3) only guards *exhaustion*
  (max-turns/timeout) loops, **not** clean-but-unproductive ticks, so nothing
  bubbled. **Immediate fix:** the tex authoring fixes on this branch let the
  LLM actually write the sections → the task progresses; verify after deploy.
  **Defense-in-depth — DONE:** nursery now has a `plan-tick-spin` detector — a
  parent minting > `PLAN_TICK_REMINT_24H` (16) `plan_tick` jobs in 24h raises a
  `warn` `kind='alert'` (`nursery:plan-tick-spin`), mirroring the `ref_events`
  spin-loop detector, so a stuck planner surfaces even though the resume-streak
  cap can't catch a clean-but-unproductive loop.

- **DONE (ops) — redeploy embedder-warmup race.** `scripts/deploy` failed once
  per run on whichever host's bge-m3 was mid-warm when the `/healthz` gate
  checked. Fixed in `~/work/cluster`: the `Install precis-mcp[embed]` git-pip
  task now retries (3× / 10s) so a transient git/wheel hiccup on one host doesn't
  fail the whole redeploy; the `/healthz` gate windows widened 40→80 (≈4 min) on
  both macOS + Linux, and the embedder-role probe 10→20 (≈1 min), covering a cold
  warm on a slower Mac.

### Residuals parked from the 2026-07-04 session (persisted; not in-reach fixes)

The confusion-mining root causes are all fixed + deployed. These remain — none
is a bounded correctness fix, so they're filed, not chained:

- **Chunk-handle (`pc<id>`) of a merged paper doesn't redirect** (design
  limitation, not a bounded fix). `resolve_handle` follows `superseded_by` for
  *record* handles (`pa<id>`) only; a merged paper's chunks are soft-deleted and
  the survivor has *different* `chunk_id`s, so there's no clean chunk→chunk
  remap. Low frequency (link/handle to a merged paper's specific chunk). A real
  fix would need a chunk-level supersede mapping at merge time — investigate
  before building.
- **`plan-tick-spin` detects but doesn't auto-pause** (behavior extension). The
  new nursery detector *surfaces* a spinning planner as an alert; it doesn't halt
  the parent, so it keeps burning ticks until acted on. Auto-pausing (e.g. an
  `open` tag the doable view excludes, like `child-failed`) would stop the burn —
  but risks halting legitimate long-running planners and needs a
  progress-signal, not just a count. Backlog, not this session.
- **Ops: cull orphaned tex refs from the nanotrans_auto spin.** The spin created
  dozens of duplicate `\section{…}` refs with `workspace=∅` (never attached to
  the project). Prod data hygiene — a one-off cleanup query, not a repo bug.

## 🔵 Tool-friction reflection + dream diversification (2026-07-04)

Spec: [`docs/design/tool-friction-reflection-and-dreams.md`](docs/design/tool-friction-reflection-and-dreams.md).
Idle-time self-improvement. Part A + the Part B lens seed are **built**
(`utils/friction_reflect.py`, `utils/dream_seed.py`); the rest is filed.

- **Part A — end-of-run tool-friction reflection → BUILT, default-OFF.**
  `utils/friction_reflect.py` appends a terminal binary-first "did any
  tool get in your way?" footer to `--append-system-prompt` at the
  `utils/claude_agent.py` chokepoint, gated on `PRECIS_FRICTION_REFLECT`
  + MCP present + `--max-turns >= 8`. "friction: none" is the honored
  default; a genuine fumble files one `friction`-tagged gripe. Ships
  **off** because once on it rides *every* production agentic run
  (planner/reviewers/dream) — enable deliberately, like the classifier.
  Residuals below.
- **Part B lens seed → BUILT, then REHOMED (shipped d7368c28,
  2026-07-05).** The single-stance persona lenses moved out of
  `data/dream_lenses.yaml` into first-class **oracle traditions**
  (`data/oracle/{scientists,leadership,artists}.yaml`) and are drawn via
  a named **lens** policy (`utils/oracle_lens.py`): the dream's default
  `sci` lens draws 50% from `scientists` and 50% evenly across the other
  traditions (even across *traditions*, not entries). `dream_lenses.yaml`
  now holds only the Disney **process** lens (multi-phase, doesn't fit
  the oracle one-block shape); the worker runs it on a fraction of cycles
  (`PRECIS_DREAM_PROCESS_PROB`, default 0.15). Round-robin coverage gave
  way to random-with-a-diversity-floor. The oracle's randomness is now
  documented as *p-hacking made honest*. `get(kind='oracle',
  args={'lens': ['sci']})` exposes the draw on the agent surface.

### 🟢 Orphaned oracle refs from boot-time re-ingest race — FIXED (2026-07-06, Opus-authored)

**Surfaced while verifying the persona→oracle deploy (d7368c28).** Prod
carried **13 orphaned `kind='oracle'` refs** — live (`deleted_at IS NULL`),
*no* `cite_key` (so `slug=None`), each a full duplicate of a real tradition
with all its blocks. The 2026-07-05 deploy produced a clean 12; the orphans
were pre-existing debris from the **07-04 00:12** and **07-05 12:20**
oracle-corpus changes.

**Root cause — three compounding bugs (all now understood + fixed):**
1. `jobs/oracle_sync` took a **session-level** `pg_try_advisory_lock` on a
   pool connection returned immediately — through **pgbouncer `pool_mode =
   transaction`** the lock strands on a recycled backend and re-acquires
   *re-entrantly* (false success) → **zero mutual exclusion**, so all 4
   post-deploy boots re-ingested concurrently.
2. `insert_ref` attaches the cite_key with `ON CONFLICT (id_kind,id_value)
   DO NOTHING` — so a racing loser's ref + blocks commit but its cite_key
   is *silently dropped* → the slug-less orphan (rather than a safe
   unique-violation rollback).
3. `ingest_paper(overwrite=True)` did DELETE-ref + INSERT-new-ref → a fresh
   `ref_id` every corpus bump, which also **churns the `or<id>` handle and
   dangles any citation/link to an oracle entry** (an independent bug).

**Fix (shipped this session):**
- **A — real lock + atomic tx.** `oracle_sync.maybe_reingest` now takes
  `pg_try_advisory_xact_lock` inside one `store.tx()` spanning the whole
  re-ingest (transaction-scoped → pinned to the tx backend → works through
  transaction pooling; auto-releases on commit; loser bails). State markers
  write on the same tx (`_write_state_conn`) so a crash can't leave the
  marker ahead of the data. Non-PG stores (test stubs, `pool is None`)
  degrade to the direct path. Old `_try_advisory_lock` /
  `_release_advisory_lock` removed.
- **C — idempotent in-place overwrite.** `ingest_paper` now `update_ref` +
  `DELETE chunks` + re-insert under the **stable ref_id** (cite_key never
  moves). Fixes handle-churn *and* means a race converges on one row
  instead of orphaning. Tests: `test_overwrite_keeps_ref_id_stable`,
  `test_shared_conn_ingest_is_atomic`, `test_advisory_xact_lock_sql_is_valid`.
- Skipped **B** (loosening `insert_ref`'s shared `ON CONFLICT` — too broad;
  C means the oracle path never re-claims a cite_key anyway).
- **Prod cleanup done:** the 13 orphans soft-deleted 2026-07-06 (reversible
  `UPDATE refs SET deleted_at=now() WHERE kind='oracle' AND deleted_at IS
  NULL AND NOT EXISTS(cite_key)`); prod now 12 clean traditions, 0 orphans.

**Follow-up — DONE (2026-07-06).** `workers/paper_reconcile.py` used the
*same* pooler-unsafe session-`pg_try_advisory_lock` pattern (a dedicated
*autocommit* connection, so every statement was its own transaction and the
lock-holding backend recycled immediately). Converted to
`pg_try_advisory_xact_lock` held inside one open transaction spanning the
whole pass (transaction-scoped → pinned through pooling, auto-releases on
commit; the dedicated conn only holds the lock, the reconcile work runs on
the `store` pool). Non-corrupting either way (the reconcilers are
idempotent), but now the single-runner guarantee is real.

### Residuals (filed 2026-07-04)

- **Enable Part A in prod.** Flip `PRECIS_FRICTION_REFLECT=1` on the
  agent-profile worker (melchior) once the downstream grouping/dedup
  lane exists to absorb `friction` gripes — otherwise raw wishes pile
  up untriaged. Gauge junk-rate; dial `--max-turns` floor if the
  planner's budget suffers.
- **Gripe → agentlog link (Part A).** The spec wants each `friction`
  gripe linked to the run's 30-day `agentlog` (model + transcript). The
  filing agent doesn't know its own agentlog id at `put` time, so this
  needs post-hoc stitching (join `friction` gripes to agentlogs by
  time+source) — or an agentlog id threaded into the run context.
  Currently the gripe self-tags `friction-model:<model>` as a stopgap.
  Confirm too that every *eligible* run emits an agentlog to link to
  (the web follow-up path may not).
- **Dream mode rotation (Part B).** Rotate the cycle's *deliverable*
  (connection / library-gap / open-question / consolidation /
  analogy-transfer), not just the lens. Deferred: it needs
  deliverable-logic surgery on `dream-prompt.md` (the connection shape
  is currently hardcoded into Step 6). Lens rotation shipped first as
  the low-risk half.
- **Deferred — active dreams (DFT / CAD / compute lanes).** *We want
  this, not yet.* An `active-build` dream mode that kicks a derived-lane
  job (DFT relax on the GPU node, `cad_propose`, structure relax) on a
  subject its wandering surfaced, then connects the *result* back into a
  memory — turning idle time into speculative build progress. Gate
  behind the load ceiling + a budget cap; derived jobs are
  content-addressed (ADR 0044), so a re-request is a cheap cache hit.

### Residuals parked from the paper-dedup/hygiene/resolve session (shipped ea7ac1ac)

Byline search + dedup Phase 3 + `paper_reconcile` (reconcilers + hygiene heals)
+ Bucket B resolver are shipped & deployed. Follow-ups, ops-gated (not repo bugs):

- **Run Bucket B on prod.** `precis resolve-metadata` (dry-run) on-cluster over
  the 94 `needs-triage` — inspect the auto/review/discard lanes, then `--apply`.
  Network-bound (Crossref/S2), so it can't be exercised from the dev sandbox.
  Expected shape from analysis: ~20 DOI-track + up to ~40-ish title-track auto,
  the rest review/discard. Book-cruft (5) + held-without-chunks (4) print for a
  human soft-delete decision. Runs on-cluster only.
- **`paper_reconcile` first prod pass** self-heals on its 24h cadence: retires
  3 dup-of-held id-less stubs (3 more to review), rebuilds ~173 drifted cards,
  collapses 1 superseded chain, migrates 2 dangling links. Watch the first pass
  in `/var/log/precis-worker.log`; no action unless it logs failures.
- **Standing worker for future id-less stubs** (Bucket B track 2 as a pass, not
  just the one-shot CLI) — build after the CLI proves the resolution on prod.
- **id-bearing stubs that title-match a held paper (49)** are deliberately NOT
  auto-merged (an authoritative id asserts distinctness). Real merges among them
  need cross-id (S2) equivalence proof → review lane, future work.

## 🔵 Platform-specific test bugs (Windows + macOS Python 3.12)

**Status**: open
**Severity**: polish
**Owner**: `tests/test_python_handler_writes.py`,
`tests/test_python_runtrace.py`,
`tests/test_python_config_wire.py`
**CI workaround**: `continue-on-error` on the affected matrix legs
in `.github/workflows/check.yml` (Linux + macOS-3.11/3.13 still
gate the release).

**Windows** — 27 tests fail because the python-handler write path
opens directory FDs with `os.O_DIRECTORY` for fsync, and that
constant is Unix-only:

- `test_python_handler_writes.py::*` (26 tests) —
  `AttributeError: module 'os' has no attribute 'O_DIRECTORY'`.
  Fix: branch on `sys.platform`; on Windows, fall back to a
  no-op fsync (or open the parent file by handle).
- `test_python_config_wire.py::test_parse_expands_tilde` —
  test asserts `~` expands to a Linux-style path; Windows expands
  to `C:/Users/runneradmin`.  Fix: assert against
  `os.path.expanduser("~")` instead of a hardcoded prefix.

**Python 3.12 setprofile + urllib.parse circular import** — 5
runtrace tests fail because the spawned tracer subprocess raises
`AttributeError: partially initialized module 'urllib.parse' …
(most likely due to a circular import)`.  First spotted on
`/Library/Frameworks/Python.framework/Versions/3.12/`; as of
2026-05-22 also reproduces in the Linux ``precis-dev`` container's
Python 3.12.  3.11 and 3.13 are unaffected; Homebrew Python 3.12
also works.  Suspect: `sys.setprofile` hook intercepts an internal
``urllib.parse`` import during a partially-initialised module
state when the user entry triggers ``argparse`` (which lazy-imports
urllib for help-text fallbacks).  Likely fix: defer the profile
install until after ``urllib.parse`` has been imported by the
bootstrap, or run the tracer in a fresh interpreter via ``-S`` +
explicit ``site.main()``.

The five subprocess-spawning tests carry
``@pytest.mark.xfail(strict=False)`` gated on Python 3.12 so they
still execute (we notice an XPASS on a non-bugged interpreter)
but don't fail the suite on bugged ones:

- ``tests/test_python_runtrace.py::test_runtrace_captures_call_tree``
- ``tests/test_python_runtrace.py::test_runtrace_argv_is_forwarded``
- ``tests/test_python_runtrace.py::test_runtrace_collapses_stdlib_by_default``
- ``tests/test_python_runtrace.py::test_runtrace_expand_stdlib_keeps_full_tree``
- ``tests/test_python_runtrace.py::test_runtrace_max_events_truncates``

Both clusters are tracked here so we don't lose them between
release and the post-release patch window.

## 🔵 OQ-11 — verify FastMCP server-pinned-prompt support

**Status**: open (verification only; design ships either way)
**Severity**: polish
**Owner**: `src/precis/mcp_modalities.py::register_skill_prompts`
**Plan artefact**: `docs/design/mcp-cold-start-token-budget.md` §Open questions
**Test**: none yet

Phase 3 of the MCP session-ergonomics rollout
(`PRECIS_STARTUP_SKILLS`) tags pinned skills on `prompts/list` and
also surfaces them via a `Pinned skills:` line in
`serverInfo.instructions` as a belt-and-suspenders fallback. The
question is whether MCP 2025-06-18 + FastMCP 1.x lets a server
flag a `prompts/list` entry as "render at session start", or
whether the tag is purely a client-side convention.

Action: read FastMCP source for `prompts/list` handler shape,
read MCP 2025-06-18 §prompts. Either way the design ships — the
banner notice carries the discovery channel — but the answer
determines whether we can stop carrying the redundant banner
line in a future cleanup.

## ⏸️ Snoozed — blocked upstream (recheck dates)

Real but unactionable until an upstream unblock. Each entry carries a
machine-parseable `Recheck-after: YYYY-MM-DD` and an `Unblock-when:`
condition. `/whatneedsdoing` reads this section and **suppresses** a
matching Dependabot alert until its recheck date, then resurfaces it as
"recheck due" for a re-probe (act, or re-snooze +2 weeks).

- **Dependabot #44 — `transformers` <5.3.0 RCE (high).**
  `Recheck-after: 2026-07-18`.
  `Unblock-when:` `marker-pdf` drops its `transformers>=4.45.2,<5.0.0` cap so
  that `transformers>=5.3.0` resolves. Today **every** `marker-pdf` (≤1.10.2)
  pins `transformers<5.0.0`, and precis needs marker in the `[paper]` extra,
  so `transformers>=5.3.0` is **unsatisfiable** — `uv lock --upgrade-package
  transformers` stays at 4.57.6, and forcing `>=5.3.0` makes the whole
  resolution fail. So the fix requires bumping **both** transformers *and*
  marker; it cannot land as a lockfile bump alone.
  **Why it's tolerable meanwhile:** the exploit surface here is ~nil — precis
  only ever loads the trusted local **bge-m3** embedder, never a user-supplied
  model path or `trust_remote_code`, which is what these `transformers` RCEs
  require.
  **When it unblocks** the bump is a *major* 4→5: validate a sample re-embed
  for cosine drift before trusting mixed old/new vectors, and if material,
  re-embed via an embed-model-version bump so the `embed` worker re-claims the
  corpus (keywords self-heal the same way via `KEYWORDS_VERSION`). Stored rows
  are never corrupted by the bump — the only risk is old-vs-new vector
  comparability.
  **Recheck procedure (on/after the date):** re-run `uv lock
  --upgrade-package transformers`; if it now reaches ≥5.3.0, take the fix →
  `/go`; if still capped by marker, bump `Recheck-after` +2 weeks.


## 🔵 Paper-ingest `equation` chunk kind — retire later (2026-07-05)

**Status**: deferred · **Severity**: feature · **Owner**:
`ingest/marker.py`, `ingest/pipeline.py`, `ingest/literature.py`

Companion to the **draft** equation-kind retirement (drafts→`$$…$$`
paragraphs, done on `worktree-mission-doc`). The draft work deliberately
left the **paper** side alone. Prod split as of 2026-07-05: **54,636**
`equation` chunks belong to `kind='paper'` (99.5%) vs only 278 to drafts —
so the `equation` `chunk_kind` is overwhelmingly a *PDF-ingest* artifact,
minted by the Marker path (`ingest/marker.py:_classify` → `pipeline.py:99`
Marker-type map), not the draft importer.

**Why it wasn't folded into the draft retirement:**
- **Different reader.** Papers render as the two-pane **PDF** reader
  (`routes/papers.py` + pdf.js), *not* the prose/chunk reader — so the
  "equation renders as raw `<p>`, not KaTeX" motivation doesn't apply to
  papers at all.
- **Deliberately un-embedded.** `ingest/literature.py` lists `equation` in
  `SKIP_EMBED_TYPES` ("LaTeX/MathML doesn't embed well with text models"),
  so paper equation chunks carry NULL embeddings by design. Migrating 54.6k
  of them to `paragraph` would either leave odd un-embedded paragraphs or,
  if embedded, dump 54k LaTeX blobs into the search index — a retrieval
  regression + a large embed load. Reopening that requires deciding the
  embed policy first (strip-to-placeholder? keep skipping? a `math`-marker
  paragraph the embedder skips?).

**If/when taken:** decide the paper-equation embed policy, change the Marker
ingest classification + `SKIP_EMBED_TYPES`, batch-migrate the 54.6k paper
chunks (throttle any cascade), then the `equation` slug can finally be
`deprecated_at`-stamped in `chunk_kinds` once *no* live chunk of any owner
kind carries it. Until then the FK row stays alive for the paper path.


## 🔵 CAD — spoked-wheel spokes don't bridge rim↔hub + no job-log link on the page (2026-07-06)

**Status**: open · **Severity**: feature · **Owner**: `cad/` (geometry
authoring / connectivity), `precis_web/routes/cad.py` (job-log link)
· **Reported on**: `/cad/make-a-spoked-wheel-with-a-mounting-bracket-v2`

Two separate issues surfaced from one CAD page:

1. **Spokes don't connect the rim to the hub.** In the renderer, the glTF,
   and the exported SCAD each spoke penetrates the rim and sticks out both
   sides while never reaching the hub — it reads as "a ring with spikes,"
   not a wheel. The connectivity lint agrees: *"2 disconnected bodies:
   wheel+bearing | hub."* The model's spoke op is
   `spoke  cyl:r2.5h28  polar n16 r26 z` (16 spokes, radius 26, axis z),
   but the rim is `torus:R40r6` (major radius 40) and the inner hub is
   `cyl:r12h16` — so a spoke centred at r=26 spanning ±14 reaches neither
   the rim wall (~34–40) nor the hub outer wall (12). This is a
   model-parameterisation problem (the edit-by-prompt / propose step
   authored geometry that doesn't span the gap), possibly worth a
   spoke-radial-length lint or a connectivity check fed back into the
   propose loop so a disconnected result is caught before it lands.
2. **No link to the failing job from the CAD page.** The page shows
   "answer failed — see the job log" (job r50911) but renders no link to
   that job, so there's no click-through to the forensics. The CAD route
   should surface a link to the owning job (`/cad/<slug>` → job r50911's
   log) when a propose/derive step fails.

---

## 🔵 OA acquisition + structured ingest + external search (2026-07-06)

**Status**: open (roadmap; nothing built) · **Severity**: feature · **Owner**:
`workers/fetch_oa.py`, `ingest/`, search/discovery layer

Root diagnosis from three "it's OA but we don't have it" reports
(`10.1002/open.70197` ref 50597, `10.1101/2024.09.13.612990` ref 50559,
`10.1126/sciadv.adx3969` no stub). All three are genuinely OA; the common
wall is **publisher-side Cloudflare/anti-bot `403`** on `onlinelibrary.wiley.com`,
`biorxiv.org`, `science.org` — the fetcher's `_BROWSER_UA` doesn't pass. The
aggregators either expose no direct PDF URL or point at the Cloudflare-gated
landing page. This is why Reto pulls them by hand via the UoL library proxy.
**Key discovery:** 2 of the 3 are in the **PMC OA subset** (Wiley→PMC13130153
CC-BY, Sci Adv→PMC12787524 CC-BY-NC) — freely + legitimately downloadable from
NCBI/EBI infra with **no Cloudflare**. So the biggest win needs no proxy and no
librarian. (Sandbox egress blocked FTP/some HTTPS here; prod cluster nodes have
open egress — the existing `europepmc` leg already succeeds 104× there.)

Interdependent items (the structured-ingest ones ride on the fetch legs):

1. **PMC OA / Europe PMC fetch leg** *(keystone — do first).* DOI→PMCID
   (`pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/`) → PMC OA service
   (`.../oa/oa.fcgi?id=PMCID`) → download the OA package (`.tar.gz`: JATS XML +
   figures + **supplementary**) or `oa_pdf`. Order it *ahead* of the
   Cloudflare-gated legs. Fixes the current `europepmc` leg's flaky
   `?pdf=render` path. Immediately lands ref 50597 + a to-be-acquired sciadv stub.
2. **bioRxiv/medRxiv S3 leg** — for `10.1101` preprints not in PMC: bioRxiv API +
   AWS `s3://biorxiv-src-monthly/` (requester-pays). Note ref 50559's VoR *is*
   the Sci Adv paper (#1 covers it) → add preprint→VoR dedup.
3. **Paid Web-Unlocker proxy leg** — last resort for Cloudflare-only-OA not in
   PMC/S3 (Zyte API / ScraperAPI / Bright Data / Oxylabs; pay-per-success,
   real-browser fingerprint). Config-gated, **off by default**. ToS caveat: this
   evades bot protection — defensible for CC-licensed content we're entitled to,
   never for paywalled. **No Sci-Hub** (copyright infringement; won't build on it).
4. **Supplementary / methods ingestion** — the PMC OA `.tar.gz` already bundles SI;
   design the storage shape (child refs linked `has-supplement` vs extra chunks
   under the paper). Same embed pipeline.
5. **JATS/XML structured ingest** — clean seam: a `extract_blocks_jats(xml, paper_id)`
   emitting Marker's block-dict shape (`{node_id,page,type,text,section_path,…}`,
   `marker.py:415`) reuses the whole downstream (`_blocks_to_chunks` →
   `_retag_references` → `_build_cards` → `write_paper`) + the NULL-embedding
   cascade + `mathnorm.normalize_math()` for MathML→`$$…$$`. **Phase 1** (new
   papers, prefer-XML, keep PDF for the reader/`pdf_sha256`) is low-risk.
   **Phase 2** (re-ingest existing PDF-Marker papers) is a registered *reversible*
   ref-pass — **hazard:** citations anchor by string `source_handle="slug~ord"`
   in `refs.meta` (not an FK), so a re-chunk restales them; must **reanchor by
   `source_quote` text**, snapshot old chunks at *ref* scope (chunk_events cascades
   away), add an `ingest_source` marker column (none today), and gate/prioritize
   (re-embed cost). **Phase 3** — stable per-chunk `handle` + citation-by-quote so
   re-chunks stop destabilizing anchors. Wrinkle: JATS has no pages → synthetic
   `page_first/last`, coarser PDF-highlight anchoring.
6. **Parallel scholarly-graph providers** — S2 alone under-covers chemistry/materials
   citation edges. Fan out to `{OpenAlex, Crossref, OpenCitations, Europe PMC, Lens}`
   and **RRF-fuse** (rank-based → robust to cross-lingual score gaps), dedup by
   DOI→title-fuzzy. OpenAlex + Crossref clients already exist (fetch legs);
   promoting them to *search/graph* providers is low effort. Lens adds paper↔patent
   linkage (synergizes with `patent` kind). Two modes: discovery-search + citation-
   graph-edge-union. Same fan-out-and-fuse subsystem as #7/#8.
7. **Chinese-lit abstract discovery** — scope to *abstract-level* discovery via
   OpenAlex/Crossref (they index Acta Chimica Sinica 化学学报 etc. with Chinese
   abstracts + DOIs) + translation; **not** CNKI full-text scrape (paywalled,
   anti-bot, low ROI — the frontier is English-first per Reto's research).
8. **Historical & foreign-language archive import** — distinct *bulk, scan-derived,
   identifier-less* class (not per-DOI). Parts: bulk fetcher (Internet Archive
   `internetarchive` lib / HathiTrust / J-STAGE) · **copyright-era gating** (pre-~1930
   PD = full-ingest; in-copyright = index/abstract-only) · **specialized OCR tier**
   (Fraktur/Cyrillic/CJK; prefer IA hOCR, re-OCR on low confidence) · historical
   cite-key identity (vol/page/year, title-fuzzy fallback). **Pilot: German
   *Chemische Berichte* (1868–1997) via IA + HathiTrust** — largest coherent PD run,
   OCR done, direct ancestor of MOF SBU/single-site catalyst theory. Legit routes
   only (IA/HathiTrust/J-STAGE); **no Sci-Hub**; CNKI = East View / institutional.
9. **Measure bge-m3 cn↔en placement for technical content** *(Reto's explicit
   ask — measure, don't assume).* bge-m3 is genuinely strong at en↔zh (BAAI =
   heavy Chinese training) and already multilingual, so cross-lingual retrieval
   works *in principle* without translating the corpus — but two open questions
   for *technical* content: (a) specialized chemistry jargon is thinner in
   multilingual pretraining; (b) the language-clustering bias systematically
   lowers cross-lingual cosine, so relevant zh chunks can be pushed below en in a
   single fused list (RRF-per-language-pool mitigates). **Probe:** hit the live
   embedder (`POST /embed`, port 8181, `embedder_service.py`) with N zh technical
   abstracts + their English equivalents; report cross-lingual vs same-language
   cosine gap + top-k retrieval of the zh chunk against distractors. Numerics/
   formulae/Latin-script terms are language-agnostic anchors and should help.

Suggested next step: one `docs/design/` roadmap doc capturing all nine with the
dependency graph (structured-ingest → fetch legs; #7/#8/#9 share the multilingual
layer), then build #1 (the keystone).

### 2026-07-08 update — MDPI/Akamai wall, OpenAlex Content co-keystone, the bulk arm

New evidence from a batch of "failed to ingest" reports (refs 53423, 53481,
53495, 53533, 53536, 53537 — mostly MDPI `10.3390`, plus IOP/APS). **Diagnosis
confirmed + sharpened, and the keystone re-ranked.**

**A. The wall is publisher-agnostic and PMC does NOT rescue MDPI.** MDPI's PDF
host `www.mdpi.com` is behind **Akamai** bot management (error page cites
`errors.edgesuite.net`), returning a hard `403 Access Denied` to *every* client
we can field — polite UA, full Chrome UA, complete browser header set + HTTP/2 —
from both a laptop IP and cluster nodes (caspar/melchior). It is TLS/fingerprint/
IP-reputation, **not** a User-Agent gate, so the `_BROWSER_UA` idea is dead for
this class. Worse: MDPI *Chemosensors* (53423) is **not in PMC** ("Identifier not
found"), **not in Europe PMC** (0 hits), CORE returned a bogus id, and DOAJ only
points back to the blocked MDPI page. So roadmap **#1 (PMC-OA leg) whiffs on the
MDPI inflow entirely** — PMC is biomedical, MDPI's chemistry/materials/eng
journals aren't indexed there. We need a route that isn't the publisher host and
isn't PMC.

**B. OpenAlex Content API is that route — verified against 53423, promote to
co-keystone (new roadmap #1b).** OpenAlex caches full-text content and serves it
from `content.openalex.org`, *not* the publisher. For 53423 the keyless metadata
already advertises it:

```
has_fulltext: True
has_content: {'pdf': True, 'grobid_xml': False}
content_urls.pdf: https://content.openalex.org/works/W4386410574.pdf   (401 w/o key)
```

So the leg is: read the (free, keyless) work metadata → check `has_content` →
if `grobid_xml` fetch the **TEI** (structured; feeds #5), elif `pdf` fetch the
**PDF** (Marker path) — both from `content.openalex.org`, a fixed trusted host
(safe_fetch still applies). **This kills the whole Akamai/Cloudflare-403 class in
one publisher-agnostic leg** instead of the per-publisher whack-a-mole of #1/#2/
#3. Key-gated (`PRECIS_OPENALEX_API_KEY`) + ~$0.01/file, but gated by
`has_content` so we only pay on a hit. **Cascade order:** free legs first
(publisher-deterministic → PMC-OA JATS → arXiv → Crossref/OpenAlex `oa_url`, all
$0, version-of-record), then **OpenAlex Content as the first *paid* fallback,
ahead of the web-unlocker proxy (#3)** which is costlier and ToS-greyer. Gate the
paid leg with a per-pass budget cap (mirror the cost caps on the agent legs). A
later optimization: when the only `oa_url` is a known-blocked host
(mdpi/wiley/science), short-circuit straight to the Content leg.

**C. XML/TEI vs PDF — the decision (generalizes #5 Phase 1).** OpenAlex content is
**PDF and/or GROBID TEI** — *not* publisher JATS and *not* LaTeX source.
- **Prefer TEI for text/chunks/embeddings when present; still store the PDF.**
  TEI → the `extract_blocks_*` seam (#5), skips the expensive/GPU/OCR Marker
  step, gives clean `section_path` + MathML→`$$` via `mathnorm`. But GROBID TEI
  is itself PDF-derived (parse errors happen) and **has no page geometry**, so a
  TEI-only ingest loses the reader's PDF-highlight anchoring (the #5 "JATS has no
  pages" hazard). Therefore fetch **both** when both are cached: PDF for the
  reader + `pdf_sha256` + highlight coords, TEI for the structured blocks. If only
  PDF is cached (like 53423), Marker it as today.
- **TeX/LaTeX source is a different route** — neither OpenAlex nor S2ORC ships it;
  the only real `.tex` source is **arXiv e-print tarballs**
  (`arxiv.org/e-print/<id>`), relevant only for arXiv preprints. Note, don't
  build now.

**D. The bulk arm (Reto's ask — "set up for a big pass") — unify with the
historical/foreign-language importer (#8).** On-demand fetch legs and bulk
snapshot import are two *front ends* onto the same `chunks → embed` backend. The
bulk front end wants a shared **bulk-ingest substrate**, and the Russian-lit +
old-German (Chemische Berichte, #8) importers are the *same shape* — bulk,
identifier-messy, non-per-DOI, structured-or-scanned text that must skip the
chase→fetch→Marker path. Common machinery:
- **Free vs paid — the key money fact.** OpenAlex ships TWO products: the **data
  snapshot** (S3 `s3://openalex --no-sign-request`, **FREE**, ~monthly) is
  **metadata only** — titles/authors/citations/OA-status/`has_content`/`oa_url`/
  abstract-inverted-index, **no body text**. OpenAlex **full text** (PDF + GROBID
  TEI, ~60M works) is the **PAID** per-file Content API (§B), *not* in the free
  snapshot. So the free bulk-full-text routes are **S2ORC** (S2 Datasets API,
  needs a key, no per-file charge) and **CORE** (`fullText` + bulk dump, free
  key). The architecture this implies: **OpenAlex free snapshot = the planning/
  index layer** (mine it to decide what+priority — this is where §E's "things we
  already have stuff on" is computed); **S2ORC + CORE = the free bulk full-text
  backbone**; **OpenAlex Content API (paid) = gap-filler for the blocked residual
  S2ORC/CORE miss** (the MDPI/Akamai case), pay-per-file only on the tail.
- **`BulkSource` adapters — the roster** (one per corpus; each yields a normalized
  "document = metadata + (pre-parsed text | TEI/JATS | to-OCR scan)"). **Build
  order: S2ORC first** (biggest free full-text win, feeds the pilot), then CORE,
  then the scan sources.
  1. **`s2orc`** — Semantic Scholar S2ORC / S2AG **Datasets API**: bulk
     machine-parsed full text, sharded gzipped JSONL, continuously updated. Needs
     a free S2 key; **no per-file charge**. *The priority-one adapter + the "big
     pass" backbone.*
  2. **`core`** — CORE (core.ac.uk) aggregates OA **full texts from ~10k
     repositories worldwide** (~300M works). Two modes: the **REST API**
     (`fullText` field + `downloadUrl`, free key — per-work, the existing
     `fetcher:core` leg's richer sibling) and the **bulk data dump** (the whole
     corpus as a snapshot — the *bulk-harvest* mode for this arm). OCR-ish
     plaintext; the broad green-OA net S2ORC misses. *Second after S2ORC.*
  3. **`oai_repositories`** — direct harvest from institutional / disciplinary
     repositories via **OAI-PMH** (`ListRecords`, incremental by datestamp) or
     their REST APIs: **Zenodo**, **PubMed Central OA**, **arXiv**, the **UoL /
     university repositories**, and disciplinary archives. This is where CORE's
     coverage or freshness lags — go to the source. OAI-PMH gives Dublin-Core /
     often JATS metadata + a link to the PDF/XML; a generic OAI harvester +
     per-repo endpoint list is the reusable core (dedup by DOI/handle against the
     corpus, copyright-gate as below). Complements #2 (CORE *aggregates* these;
     direct harvest gets what it hasn't indexed and stays current). arXiv here is
     the *bulk* path (the per-DOI arXiv leg already exists in `fetch_oa`).
  4. **`openalex_snapshot`** — free S3 metadata snapshot (`--no-sign-request`).
     **Index/planner only, NOT a full-text source** — mines *what* to ingest and
     in what priority (feeds §E); the paid Content API (§B) is the per-file
     full-text gap-filler, a *separate* thing.
  5. **`internet_archive` / `hathitrust` / `jstage`** — scan-derived corpora for
     the historical/PD run (#8; old-German *Chemische Berichte* pilot). hOCR in,
     OCR tier for low-confidence.
  6. **`east_view` / institutional** — Russian-lit full text (paywalled/licensed;
     copyright-gated per below).
- **Structured-text → blocks** — reuse the #5 `extract_blocks_*` seam so S2ORC
  JSON / TEI / JATS / IA hOCR all land in Marker's block-dict shape → existing
  `_blocks_to_chunks → _retag_references → _build_cards → write_paper` → NULL-
  embedding cascade. **Skips Marker entirely** (text already parsed) except the
  scan/OCR tier.
- **Dedup + identity** — DOI → title-fuzzy → cite-key against the live corpus
  (reuse `dedup.py` / `paper_hygiene`), so a bulk pass folds into held/stub refs
  instead of minting millions of duplicates.
- **Copyright-era gating** (from #8) — pre-~1930 PD = full-ingest; in-copyright,
  non-OA = index/abstract-only. CC-licensed OA = full.
- **Specialized OCR tier** (from #8) — Fraktur/Cyrillic/CJK; prefer source hOCR,
  re-OCR on low confidence.
This is a **new subsystem**, not a fetch leg — the deliverable "set up for a big
pass" = land this design + the S2ORC `BulkSource` scaffold, then run a gated
pilot. **Decisions needed before executing:** (a) S2 API key availability +
storage target for a multi-hundred-GB snapshot, (b) target scale (tens-of-
thousands vs millions — sets on-demand-per-file vs bulk-snapshot), (c) chemistry/
materials-first vs broad (S2ORC under-covers MDPI/Chinese-lit; OpenAlex broadest).

**E. Embedding-prioritization — OPEN design question, deliberately NOT solved
(Reto: "let's not complete that part").** A bulk pass dumps millions of NULL-
embedding chunks; bge-m3 throughput is finite + load-gated, so naive FIFO starves
fresh on-demand papers behind the cold-import flood for weeks. Reto's instinct:
**"prioritize the things we already have stuff on."** Candidate priority signals to
weigh later — ref referenced by a todo/draft/project/citation/link (warm set);
recently viewed/searched/flagged (`last_viewed_at`, flags); explicit `PRIO` /
in-a-project; ref-creation recency (on-demand fresh > bulk backfill); topical
adjacency to existing high-signal chunks (chicken/egg — needs an embedding to
know; use cheap lexical/keyword overlap as a proxy). Mechanism sketch: an
embed-priority ordering in the claim query, bulk chunks stamped a low-priority
`meta.ingest_source='bulk'` that **trickles behind live traffic** (same principle
as `llm_summarize` on melchior). **Not a decision yet — captured so the bulk pass
doesn't ship without a queue policy.**

**F. Small concrete items found in the dig:**
- **CORE leg bug** — the 53423 log shows `fetcher:core` tried to download the URL
  `"587670336"` (a bare CORE work-id where a `downloadUrl` was expected) →
  "refusing non-http(s) URL". `_query_core_pdf_urls` now validates both
  `downloadUrl` and `fullText` are http(s) URLs before adding them to the
  candidate list. Owner: `workers/fetch_oa.py`.
  **DONE.** Test: `TestQueryCorePdfUrls::test_filters_invalid_urls_and_uses_full_text`.

**G. OpenAlex free-metadata enrichment — WANTED (Reto: "we want that meta").**
The OpenAlex *work* object is free + keyless (`api.openalex.org/works/doi:<doi>`,
49 fields) and far richer than what we hold. Slurp it into the ref — independent
of (and cheaper than) the paid content pull. Field → home:
- `referenced_works` (OpenAlex IDs of cited works, 110 on 53423) → **citation-graph
  edges** (`links` `cites`, resolvable to DOIs → link held papers / mint stubs).
  The highest-value field — it densifies the graph S2 alone under-covers.
- `authorships` → **authors** (ORCID per author + institution **ROR** + country)
  into the `authors` JSONB byline.
- `topics` / `concepts` / `keywords` → controlled **`ref_tags`** (topic axis).
- `funders` / `grants`, `fwci`, `cited_by_count`, `sustainable_development_goals`,
  `mesh` → `ref.meta`.
- `is_retracted` → cross-check `retraction_status`; register `openalex:W…` in
  `ref_identifiers`.
Home: a metadata source alongside CrossRef/S2 in `ingest/metadata_resolve.py`,
**or** a dedicated `openalex_enrich` ref-pass (idempotent upsert, polite `mailto`
pool, fixed host so no SSRF concern). Runs at stub-promotion/ingest **and** as a
backfill over existing paper refs. Same free API also = the §D
`openalex_snapshot` planner at per-record granularity (live API for a handful; the
free S3 snapshot for millions). **BUILT this session (unshipped) — see below.**
Deferred within G: `referenced_works` **edge materialization** (W-ids → DOIs →
`links` `cites`) rides on the scholarly-graph fan-out (#6) — the raw W-ids are
captured in `meta.openalex.referenced_works` now so no re-fetch is needed later;
topics→`ref_tags` waits on the OPEN-namespace teardown; wiring the backfill CLI
into a scheduled worker pass is a follow-up (the CLI covers the sweep today).

**BUILT this session (unshipped, green: ruff+mypy+targeted pytest):**
- **OpenAlex Content leg** (`workers/fetch_oa.py` `_try_openalex_content`) — the
  §B rescue, Phase 1 (PDF only; TEI deferred to #5). Reads free `has_content`,
  downloads from `content.openalex.org` with `?api_key=`, records `cost_usd`
  ≈$0.01, key never leaves the query (not in the payload). LAST in the cascade,
  **double-gated**: `PRECIS_OPENALEX_CONTENT_KEY` **and**
  `PRECIS_OPENALEX_CONTENT_AUTO` (default OFF) so it merges dark and can't
  auto-bill the backlog.
- **`precis fetch-openalex <doi|ref_id>`** (`cli/fetch_openalex.py`) — the manual
  "penny now" one-shot (bypasses the auto gate); downloads into
  `PRECIS_WATCH_INBOX`, writes the stub-fold sidecar when given a ref_id. This is
  the path to prove 53423 the moment the key + funded balance exist.
- **Failure-reason surfacing** (`store/_refs_ops.py`) — `/papers-needed` now
  renders the concrete why ("fetch failed: mdpi.com 403 — will retry in 24h")
  instead of a bare `fetch_failed`; payload threaded into `_stub_state_summary`,
  host+HTTP-status extracted. Verified end-to-end against real Postgres.
- **§G OpenAlex free-metadata enrichment** — `ingest/openalex_meta.py`
  (`fetch_openalex_work` + pure `normalize` + `enrich_ref`) writes the
  `meta.openalex` block (abstract, topics, funders, fwci, cited_by, 110
  `referenced_works` W-ids, ORCID+ROR authorships), registers `openalex:W…`, and
  fills the byline only when empty. CLI `precis enrich-openalex <doi|ref_id>` +
  `--backfill --limit N`. Verified live against 53423 (fetch+normalize) and
  end-to-end against real Postgres (write). 11 unit tests.
- **NOT yet built:** the TEI/`grobid_xml` structured-ingest path (#5), the CORE
  bug fix, the bulk arm (§D), the auto-leg budget cap for when AUTO is flipped on.
  **Verify on first real key:** OpenAlex Content auth is `?api_key=` (per their
  docs + the URL format) — confirm on the first live 200.

### Residuals — stub↔ingest dedup-split fix (SHIPPED c6152950, 2026-07-06; Opus-authored)

The "fetched 16h ago but not ingested" cards (stubs 50698/50754) were a
**dedup split**: the OA fetcher's stub and the PDF-derived identity didn't
intersect (Marker truncated the DOI, or extracted none), so ingest minted a
duplicate ref and left the stub `pdf_sha256 IS NULL`. Fixed forward with an
**acquisition sidecar** (`ingest/fetch_sidecar.py`) carrying the stub `ref_id`
so `precis_add` folds into it in place, **plus** the root-cause fix
(`_reconcile_orphan_stub` now also runs on the new-ref branch, not just the
dedup-hit branches). Residuals parked (all harvest-eligible, Opus-authored):

1. **Multi-host inbox race — spurious `no such file` errors (deliberately
   deferred).** 28/30 ingest `error.txt`/day are the 4 watchers racing the
   shared NFS inbox: the loser's Marker run dies with `FileNotFoundError` when
   the winner moves the PDF mid-extraction. The winner ingests fine (not data
   loss) but the loser writes a bogus `error.txt` — the `errors/` dir lies. The
   "vanished mid-ingest, skip silently" guard (`watch.py:619`) misses it because
   pymupdf/pdftext **wraps** the `FileNotFoundError`, so it hits the generic
   `except Exception`. Fix: recognize a wrapped file-vanished error (check
   `pdf.exists()` / walk `__cause__`) and skip silently instead of erroring.
   Owner: `cli/watch.py`. Severity: polish (noise + wasted Marker cycles).
2. **Metadata-poor extraction leaves the ref titleless.** For 50995 (`anon00ag`)
   Marker put the title in chunk 0 ("CONTINUOUS DEFORMATIONS…") but the
   **ref-level `title` stayed empty** (`[no metadata]`), cite_key degraded to
   `anon00ag`, blocking after-the-fact title-similarity reconcile. Prod
   population (2026-07-06): **187 titleless chunked papers — 32 with a DOI, 155
   with no external id**. **Do NOT** backfill title from chunk 0 (a wrong title
   is worse than none). The confident fix already exists: `metadata_resolve.py`
   (`precis fix-metadata`) never trusts PDF text as a title — Track 1 re-resolves
   CrossRef by DOI (the 32); Track 2 S2-title-searches with chunk-0's first line
   and **auto-applies only at similarity ≥ 0.85 + compatible year + recovered-DOI
   not already owned**, everything else → `needs-triage` (human). Reversible,
   source-stamped, dry-run-previewable. **Progress (SHIPPED, 2026-07-08):**
   `resolve-metadata` now (i) scans the **first ~4 body chunks** for the title
   query — not just chunk-0's first line, which is a masthead/received-line/bare
   author list ~half the time — filtering body furniture + stripping markdown,
   trying each candidate and keeping the best-similarity S2 hit (recall up,
   precision unchanged: the 0.85 gate still guards every write); and (ii) its
   `_triage_refs` cohort is **widened** to include any titleless chunked paper,
   not just `needs-triage`-tagged ones (the 135 untagged of 187 were previously
   unreachable). **Remaining:** (a) run the dry-run over the cohort → verdict
   distribution + gold-check the `auto` set, then `--apply`; (b) **schedule it**
   (manual-only today) into `paper_reconcile`/hygiene so titleless refs self-heal.
   The shipped sidecar fold means the 187 is a fixed backlog, not growing. Owner:
   `ingest/metadata_resolve.py`, `cli/resolve_metadata.py`. Severity: feature.
3. **Verify the 7 existing orphans self-heal post-deploy.** 50698, 50754, 49915,
   50223, 50227, 50335, 49503 are already split (content under duplicate refs).
   They should self-heal when `requeue_stranded_fetches` re-fetches them at >48h
   (the re-fetch now writes a sidecar → folds into the stub instead of
   re-splitting), OR immediately if re-queued now (`meta.oa_requeued`). Confirm
   the cards resolve; if a metadata-poor re-fetch (no sidecar-fold) leaves a
   residual junk dup (e.g. 50995), that's covered by #2 + a title-sim reconcile
   extension to id-bearing chunkless stubs. Owner: verify on prod.

---

_Last updated: 2026-07-08 (added the "2026-07-08 update" block to the OA section:
MDPI/Akamai wall confirmed publisher-agnostic + PMC-doesn't-cover-MDPI; OpenAlex
Content API verified reaching 53423 → promoted to co-keystone #1b, killing the
Akamai/Cloudflare-403 class in one leg; XML/TEI-vs-PDF decision (prefer TEI, keep
PDF; TeX only via arXiv e-print); the bulk arm unified with the historical/
foreign-language importer #8 (S2ORC/CORE = free bulk full text, OpenAlex free
snapshot = index/planner only, OpenAlex Content paid = blocked-residual gap-
filler) + Russian-lit; embedding-prioritization left OPEN per Reto; CORE bare-id
bug + failure-reason surfacing noted. Same day, later: made the `BulkSource`
roster an explicit named-adapter list — **`s2orc` priority-one** + `core` +
`openalex_snapshot` (index-only) + IA/HathiTrust/J-STAGE + East View; added item
**G — OpenAlex free-metadata enrichment (WANTED)** with the field→home map
(referenced_works→citation edges, ORCID/ROR→authors, topics→tags); and built
(unshipped) the OpenAlex Content leg + `precis fetch-openalex` CLI + failure-
reason surfacing). Prior: 2026-07-06 (added the stub↔ingest
dedup-split residuals block under the OA section — shipped c6152950: acquisition
sidecar + new-ref-branch reconcile; 3 residuals parked: multi-host `no such file`
race, titleless metadata-poor refs, verify the 7 existing orphans self-heal).
Prior same day:
added the OA-acquisition + structured-ingest +
external-search roadmap — 9 interdependent items from the "it's OA but we don't
have it" diagnosis: publisher Cloudflare-403 is the common wall, PMC OA subset is
the free unblock for 2/3; keystone = a PMC-OA fetch leg; incl. JATS re-ingest with
the citation-reanchor hazard + the bge-m3 cn↔en measurement Reto asked to store);
also added the CAD spoked-wheel disconnected-spokes geometry bug + missing
job-log link on the CAD page. Prior: 2026-07-05 (added the paper-ingest
`equation`-kind
retirement as deferred backlog — companion to the draft equation→$$
retirement on `worktree-mission-doc`; 54.6k paper equation chunks vs 278
draft, different reader + deliberately un-embedded, so paper side needs its
own embed-policy decision first). Prior: 2026-07-04 (added the ADR 0047
chunk-tag classifier remaining-work section — enable continuous tagging /
Tier-2 escalation / ref-axis runner / table heuristic; pruned the
Recently-retired graveyard + done CI item — both in git; snoozed Dependabot
#44 transformers RCE until 2026-07-18, blocked by marker-pdf's
transformers<5 cap)_

---

## 🔊 LaTeX → speech for voice drafts

`open` / `feature` / owner `precis/draft/narrate.py` (+ maybe a node SRE step).
The voice-draft narration layer currently **skips math** — `speakable()`
replaces `$$…$$` with a spoken "equation" cue and drops inline `$…$`. Fine for
prose-heavy drafts, weak for math-heavy ones (a graphene report is wall-to-wall
equations). Upgrade: a `math_speech ∈ {skip, brief, full}` mode.

- **Accessibility-grade** = MathSpeak/ClearSpeak via the **Speech Rule Engine**
  (SRE, the MathJax/JAWS/NVDA engine) over MathML. precis already ships
  `latex2mathml` (docx extra) so **LaTeX → MathML is in hand**; the missing step
  is MathML → speech (SRE is JS → a `node` shell-out, like the cad-tessellation
  parity check).
- **Pure-Python heuristic** (my lean for v1) — `^`→"to the power of",
  `\frac`→"over", greek letters, operators. Covers inline/simple math, no node,
  imperfect on hairy display math (which `brief` mode still elides).
- **Per-equation author override** — the pronunciation-lexicon pattern extended
  to math: an authored spoken form ("read as: the Arrhenius rate law") for the
  equations that matter. Out-of-band, abbrev-class.

Slots into `speakable()`; default stays `brief` so equation-dense sections
don't become unlistenable. Finder: Opus session (2026-07-14).

> **Context (shipped 2026-07-14):** the **news-briefing audio producer** landed —
> `workers/briefing_audio.py` narrates the daily `briefing-<date>` ref to the
> podcast feed (gated `PRECIS_BRIEFING_AUDIO_ENABLED`, TTS-host-only, idempotent
> via `meta.audio_episode_id`), plus the reusable `export.audio.synthesize_text`
> stitch helper and `narrate.markdown_segments` prose path. The **first automatic
> producer** on the audio pipe. See `docs/design/audio-feed.md`. Only the
> LaTeX→speech upgrade above remains open in this area.


## 🟠 Architecture review / compaction / footguns (2026-07-15)

**Status**: open · **Severity**: refactor · **Owner**: multiple (see per-item)

Architecture review covering ADR/documentation sprawl, code-structure overload, and operational footguns. This is a cross-cutting backlog; it is intentionally not a single PR. Security was excluded from this review.

### P0 — stop the next incident

- **Harden `build_runtime` against storeless double-build** (`runtime.py`, `secrets.py`). `adopt_process_store` scrubs `PRECIS_DATABASE_URL` from `os.environ`; any later in-process `build_runtime()` falls back to the adopted store’s DSN via `secrets.get_adopted_dsn()`. Reference: residual above (`## 🩹 Residuals`). **DONE.** Test: `test_build_runtime_falls_back_to_adopted_dsn_after_env_scrubbed`.
- **Schema reconcile must preserve PostgreSQL ACLs** (`scripts/reconcile`, `store/migrate.py`). `migra`-generated diffs do not emit `GRANT`s; new tables end up owned by `deploy` with no `agent_rw`/`agent_ro` grants. Add an ACL diff/re-grant step before marking reconcile done.
- **Worker + watch restarts are a pair** (`docs/` runbooks, `com.precis.worker`/`com.precis.watch` plists). Restarting only `watch` leaves `worker` stopped and the derived queue backlog grows. **Done**: `scripts/restart-worker-and-watch` restarts both in order with a single command; documented in `docs/runbooks/restart-worker-and-watch.md`.
- **Set `PRECIS_OPS_ALERT_WEBHOOK` on system-profile workers** (`workers/`, cluster ansible). `notify_critical_alert` now reads `PRECIS_OPS_ALERT_WEBHOOK` and falls back to `PRECIS_OPS_ALERT_TARGET`. **Done** in code; set `PRECIS_OPS_ALERT_WEBHOOK` in the cluster ansible env to page worker-restart/dead-worker alerts.

### P1 — compaction and modularization

- **Compact ADRs with a “Rest in Git” archive** (`docs/decisions/`, `docs/design/`). **Convention established**: `ADR-0058` (`docs/decisions/0058-decision-log-archive-convention.md`) + the `docs/decisions/archive/` scaffold + index wiring define the move-not-delete recipe (filename kept, one-line archive banner as the only sealed-body edit, every referrer updated in the same change; archivable only when a live successor already names the predecessor). **Numbering/grouping corrections found while scoping**: the ADR log only reaches `0058`, not `0064` (the migration numbers, which reach 0065, were conflated) — condensed head ADRs take the next free ADR number; and `0019-second-greenfield` belongs to the migration-baseline chain (superseded by `0031`), **not** the image/embedder chain. **Remaining** (each its own reviewed change, so referrer updates stay auditable): supersede each major chain with one condensed live ADR and move predecessors to `archive/`. Candidate chains: identifier (`0002/0006/0008` → head `0036`), derived queue/job (`0007/0017` → `0044`), image/embedder split (`0004/0009/0012/0019` → heads `0020/0021`), figure/asset model (`0034/0035` → `0057`), keystone kinds (`0041/0042/0043` → `0053/0056`), argument/turn-taking (`0051` ↔ `0054`). Split design-heavy ADR bodies into `docs/design/` where warranted.
- **Split `runtime.py` into per-concern modules** (`runtime/dispatch.py`, `runtime/search.py`, `runtime/angle.py`, `runtime/hints.py`, `runtime/error.py`). `runtime.py` is 2397 lines; `_dispatch_cross_kind` is 233 lines.
- **Refactor `paper.py search()` into search strategies** (`handlers/paper.py`). `search()` is 600 lines; extract `BylineSearch`, `FusedBlockSearch`, `GoodSearchCampaign`, and `PaperSearchResultRenderer`.
- **Extract `EditableFileHandler` from `draft/plaintext/python/markdown/tex` handlers**. The 160+ line `_put_anchored` methods are duplicated and diverging.
- **Split `store/_blocks_ops.py` and `_draft_ops.py` by concern** (`store/`). Split into SQL builders, search rankers/fusion, and card writers; `_draft_ops.py` alone has 72 functions.
- **Split `precis_web/routes/drafts.py` into per-concern route modules** (`precis_web/routes/drafts_*.py`). 3078 lines in one route file.

### P2 — quality and discoverability

- **Centralize `PRECIS_` env vars** (`precis/config.py`, `precis/kind_gate.py`). 381 unique `PRECIS_` strings appear in `src/precis`, but `PrecisConfig` only declares 19. Replace ad-hoc `os.environ.get` calls in handlers with `requires_env`/`requires_secret` declarations; change `PrecisConfig.extra` to `forbid` once all envs are registered.
- **Replace closure-name worker pass priority with an explicit enum** (`cli/worker.py`). `_REF_PASS_PRIORITY` keyed by `*_pass` `__name__` is brittle; a rename silently changes scheduling band. **Done**: bands are now a `PassBand(IntEnum)` (JOB/PLANNER/HEALTH/DEFAULT/BACKGROUND) and an AST-based guard (`test_ref_pass_priority_keys_match_registered_passes`) fails if any table key no longer matches a live `ref_passes.append(_<name>_pass)` site, so a rename can no longer silently re-band a pass.
- **Tighten broad `except Exception` catches** (`workers/fetch_oa.py`, `runtime.py`, `server.py`, worker loops). 317 broad catches across 141 files; many hide spin loops.
- **Enforce chunk append-only discipline at runtime** (`store/`, triggers or `ChunkOps`). The rule is convention-only; an in-place `UPDATE chunks.text` leaves embeddings/summaries stale. **Done**: migration `0065` adds a `BEFORE UPDATE` trigger (`chunks_forbid_body_text_update`) that rejects a text-changing UPDATE on a body row (`ord >= 0 AND content_sha IS NULL`), while leaving the two sanctioned in-place paths alone — draft-family chunks (non-NULL `content_sha`, sha-diff cascade) and cards (`ord < 0`, `rewrite_cards` drops their embeddings). DELETE+INSERT stays the way to replace body text. See `docs/design/chunk-append-only-trigger.md`; test `tests/test_chunk_append_only.py`.
- **Add headless-browser tests for the draft editor** (`tests/`). The inline editor and virtual scroller are not covered by automated tests.

### P3 — type/platform/debt

- **Burn down the five disabled mypy categories one by one** (`pyproject.toml`). ~184 issues across `union-attr`, `index`, `assignment`, `type-var`, `operator`.
- **Fix Windows `O_DIRECTORY` and Python 3.12 urllib circular import failures** (`tests/`). These legs are currently `continue-on-error` in CI.
- **Recheck `transformers>=5.3.0` / `marker-pdf` pin** (`pyproject.toml`). Dependabot #44 is snoozed; recheck on the scheduled date.
- **Re-evaluate `ruff` ignores `RUF012` and `B905`** (`pyproject.toml`). `RUF012` (mutable class defaults) and `B905` (zip without `strict=`) can hide real bugs.

### Related existing items

- `## 🩹 Residuals — asa storeless-precis incident` (build_runtime storeless trap).
- `## 🟠 Worker liveness + observability` (worker/watch restart / observability).
- `## 🔵 Platform-specific test bugs` (Windows / macOS 3.12 test failures).
- `## 🟠 LLM-confusion bugs mined from prod plan_tick transcripts` (plan_tick spin/optimization).
