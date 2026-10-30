# Thread programmes — the coordination map

Thread files own ranking for one thread. This page groups them into
programmes so sessions that collide on code or wait on each other can see
it in one place. Pointers only: a seam is a file or item two threads both
touch, plus the sequencing rule; a wait is an item one thread ranks and
another consumes. Update it at the same four moments as a thread file.

## Pillars

Five pillars sit above the programme layer, text in `docs/roadmap.md`.
Each programme below serves one or more:

- **memory-graph** — `knowledge` (knowledge-mesh owns the substrate) +
  `graph-memory-consumers` (owns consumers of that substrate) +
  `claims-and-evidence` (identity and evidence checks of claims) +
  `draft-authoring` (drafts, export, tex layer).
- **3d-design** — `pcb` + `se` + `chemistry` (incl. `catalysis-selectivity`) + `multiscale-design-core` + `se-machine-design` + `pcb-platform`.
- **local-compute** — `local-compute` (owns local model
  serving: the summariser and the single-spark model, and what they do).
- **personal-integration** — HELD; no programme yet.
- **operations** — `deploy-fleet-ops` · `security-hardening` ·
  `monitors-that-go-quiet` · `ship-gate-ci` · `serving` (`serving-programme`
  owns the MCP ceiling and the eval spine; `session-mcp-shared-server` owns
  the shared server); keeps the cluster, the gate and the deploy path
  working for every pillar.
- **platform** (bucket) — `plugin-split` + `factory` + `agent-surface` (MCP
  effectiveness: errors that carry the fix, misuse guards, description
  budgets, Drive); serves every pillar indirectly.

## Activity and next actions

[Generated priority table](PRIORITIES.md) — declared activity comes only from
each thread file's `roster:` front matter
(`grep -l '^roster: active' docs/backlog/threads/*.md`); it is not a
live-session check. Run
`scripts/docs-index` if missing or stale. Read a thread's Resume
first, then its current item; load other threads only for dependencies.

## pcb — design, route, fabricate, order

Threads: `ewod-pcb.md` · `pcb-easyeda-round-trip.md` · `pcb-platform.md`
(sequenced behind the other two on generator/DRC files; see seam below).

Seams (same files, different work — sequence, never merge):
- `src/precis/pcb/generators.py`, `drc.py`, `realize.py`/`maze.py` —
  easyeda's router item (pcb-missing-constraint-classes §E-1) goes before
  ewod's escape-layer leak and escape-corridor rewrite; ewod sequences
  behind by its own choice.
- `backlog/pcb-guided-place-route.md` — ewod owns it (layer count as a
  user call, the one open item).
- `backlog/pcb-missing-constraint-classes.md` — easyeda owns the router
  half now, ewod owns the HV vocabulary on its Horizon.

Waits:
- ewod Horizon 10 (`backlog/ewod-synthesis-protocol.md`) consumes
  se-nucleic-chain's make_steps (shipped 2026-09-30; the chain thread's
  Horizon 2 points back at it).
- pcb-platform is behind both active pcb threads on the shared
  generator/DRC/realizer files by the same seam rule above; it does not
  reorder either.

## se — 3D modelling, chains, hexfold, machine design

Threads: `se-3d-viewer.md` · `se-nucleic-chain.md` · `hexfold-toolkit.md` ·
`se-machine-design.md` · `multiscale-design-core.md` ·
`structure-workspaces.md`

Seams:
- `backlog/se-pick-hierarchy.md` — viewer Horizon 1 (the keystone) and
  chain Horizon 9 are the same surface; the viewer owns the selection
  mechanism and the click-time pick route, the chain thread owns the
  resolver (`precis_se/pick.py`, built) the route calls.
- precis_se atomic output — a composite corrupted by a stale process
  (gr458061) renders in the
  viewer as a wrong picture; hexfold's `composite_part_stolen` validate
  check now reports it, so the viewer's interest is unchanged, only the cause.
- se-machine-design (the se owner, active since 2026-10-01) owns the design model, including the
  non-geometric property layer (hydrophobic, charge, field, optical);
  se-3d-viewer owns rendering whatever that model carries. Model vs
  render, not a rank duplication.
- se-machine-design also owns `backlog/pcb-se-binding.md` and
  `backlog/pcb-argue-with-design.md` — the mm→m crossing between se and
  pcb — not ewod-pcb or pcb-platform.
- `backlog/structure-workspaces.md` and `backlog/pcb-workspace-code.md`
  (thread `structure-workspaces.md`; pcb-platform Do-next 1 points at it)
  run on the factory thread's `sandbox_run mode:script` lane
  (`backlog/sandbox-python-script.md` slice 4) and consumes
  hexfold-toolkit's `sheetnet` layer as its core API; it owns the import
  boundary and the relax interface, factory owns the container.
- multiscale-design-core is the substrate se-machine-design's
  model stands on (design-state-core, pattern groups, complementarity);
  se-machine-design consumes it rather than re-deriving it.
- `src/precis_flow/`, `src/precis_se/flow.py`, `backlog/se-flow-kernel.md`
  slices 4a–4b and `backlog/se-native-fields.md` — se-machine-design owns
  the physics (kernels, regime cards, `se-multiphysics.md` slices);
  structure-workspaces ranks and builds the script-facing toolkit
  (`precis_geom.flow`/`physics`/`params`/`optimize`/`fields` in the
  wheel, `evaluate`, the `precis-workspace-optimize-help` skill, the
  examples, the fields slices). A kernel change lands through
  se-machine-design (e.g. 4b's outlet-mask `evenness`); the wheel/API
  shape through structure-workspaces (seam recorded 2026-10-22).

Waits:
- viewer Horizon 3–4 (reaction forces, mechanical DRC phase 2) feed
  `backlog/se-feasibility-and-cost.md`, the far end all three serve.
- nanobuds-paper's seam-topology cross-cite waited on the `EnvKey`
  extent fix (gr456641 + gr457997), which shipped; its remaining waits
  live in its own thread file.
- td344088 (se + hexfold paper) waits on the walker dogfood (chain
  Do-next 1) for its figure.

## chemistry — pathways, catalysis, reaction facts

Threads: `chemistry.md` (pillar 3d-design; catalysis lives here, Reto
2026-10-01) · `catalysis-selectivity.md` (active 2026-10-02, Reto "high
up": fork selectivity, NH₃ network completeness, U/pH window, decisive-step
NEB, step-annotated diagram) · `nanoreactor.md` (dormant 2026-10-04:
the NO→NH₃ carbon nanoreactor toolchain). Code: `src/precis_pathway`; `../catpath` is the reference
engine.

Seams:
- `backlog/pathway-presentation-shared-module.md` — plugin-split owns it
  (its Do-next 6); chemistry's UI items (Horizon 9) consume it and start
  no second copy.
- `backlog/slullama-hpc-placement.md` §From precis-dispatch — ranked in
  local-compute; chemistry's DFT relax is the runner's first consumer, and
  its seams extract when the second backend runs.
- roadmap-quest owns the quest loop that dispatches and consumes
  pathways; chemistry owns the engine's health and output contract and, since
  2026-10-01, the catalysis quests' content (qu164903 ticking, the qu202467
  restart report).
- catalysis-selectivity edits the ammonia network content and adds link
  fields (`added`/`removed`, typed `kind`); chemistry owns the engine
  contract and the wheel bump. `backlog/catpath-desorption-link-kind.md`
  moved to catalysis-selectivity (Do-next 2); `neb-barriers-in-the-catpath-pipeline.md`
  stays chemistry's, catalysis-selectivity decides where NEB runs.
- `backlog/kinetics-engine-v2.md` (catalysis-selectivity) owns the
  kinetic environment block (T, split input/output partial pressures,
  optional U/σ/cation/pH) and the ΔG/TS provider seam; the
  electrochemical-interface spec plugs a provider into that seam and
  never redefines the rate builder. Engine code lands in catpath via
  chemistry's wheel bump.
- nanoreactor's T1 (`backlog/nanoreactor-station-path-finder.md`)
  adds off-slab environments and a station-record export to the engine;
  chemistry owns the engine contract and the wheel bump. Its T2
  (`backlog/hexfold-nanoreactor-tube.md`) is built by hexfold-toolkit.
- hexfold-toolkit owns `backlog/global-structure-search-slices.md` and
  `backlog/structure-kind-demotion.md` (structure kind, se as origin of
  atoms), not chemistry.

## serving — the MCP and the fleet's model serving

Threads: `session-mcp-shared-server.md` · `serving-programme.md` ·
`local-compute.md`

Seams:
- `backlog/session-mcp-http-server.md` — ranked only in the shared-server
  thread; serving-programme cross-references it.
- `backlog/mcp-concurrency-load-test.md` and the K-parallel harness — the
  serving thread's py-spy answer (its Do-next 1) decides whether the
  shared-server's multiprocess item is topology or workaround.
- served vs used — serving-programme owns the MCP ceiling, the load-test
  harness and the eval-run-spine; local-compute owns local model serving
  (vLLM Slice 0 and its spark prerequisites, local-serving-eval, the
  summariser) and what the served capacity does (summarise, insert, mesh,
  link, categorise). The eval-run-spine's items 4 and 9 wait on the spark
  prerequisites ranked in local-compute's Do-next 3.
  Embedder capacity (local-compute's, decided 2026-10-02) is fed by
  serving-programme's py-spy/topology answers.

Waits:
- serving Horizon 2 (shared-server end state) waits on serving Horizon 1's
  process-count answer.
- `backlog/curation-gate.md` waits on eval-run-spine's verdict column
  (serving Do-next 3) and is parked on by knowledge-mesh and roadmap-quest.

## knowledge — taxonomy, quests, papers

Threads: `knowledge-mesh.md` · `roadmap-quest.md` · `nanobuds-paper.md` ·
`claims-and-evidence.md` · `graph-memory-consumers.md` ·
`draft-authoring.md`

Seams:
- `backlog/measures-substrate.md` — knowledge-mesh Do-next 5; roadmap Horizon 4
  (meta.supply widened to measures) consumes it.
- `backlog/knowledge-mesh.md` — knowledge-mesh Horizon 3; roadmap Horizon 4 is
  its in-scope 2.
- `backlog/curation-gate.md` — both park on it; owned by serving.
- Part refs (shipped 2026-10-02, `precis.handlers.part` docstring) —
  ewod-pcb's datasheet autopull (shipped) gets `datasheet-of` for free:
  setting `part_lcsc` on the pulled datasheet writes the edge too, and
  `datasheet-facts-mesh.md` (knowledge-mesh Horizon 16) hangs the
  extracted facts on the part ref.
- `fisheye-everywhere` — landed 2026-10-12 (backlog doc deleted
  2026-10-15); the eye ladder on every kind and the `/eye/<handle>`
  focus page are the human graph-browse surface (docs/roadmap.md
  pillar 1).
- `backlog/file-mirror.md` — knowledge-mesh Do-next 7; the memory half of the
  thread (context-memory-hierarchy, session-history-into-precis) is
  ranked in `knowledge-mesh.md` from 2026-09-30.
- memory recall/walk/keep slice 1a (shipped) built the store-level
  neighbourhood function that `backlog/web-graph-navigation.md` exposes:
  slice 1 as `GET /graph/<kind>/<id>.json`, slices 2–3 (shipped
  2026-10-14) as the ref-page panel fragment and the radial-SVG focus
  page. One query, one inverse rule, one cap — the MCP memory walk and
  the web surfaces must not disagree (seam recorded 2026-10-07).
- substrate + memory half vs agent affordances — knowledge-mesh ranks the
  substrate (knowledge-mesh, measures-substrate, graph-gardener-value), the
  memory half and the surfaces; `graph-memory-consumers.md`
  ranks draft-authoring affordances, the focus verb, capability discovery,
  skill quality and source-code ingest. Do not duplicate ranking across
  the two files.

- claims-and-evidence vs knowledge-mesh — knowledge-mesh keeps the taproot
  umbrella (hub model, seniority, hub-refine) and references it; the
  defect/follow-on cluster (identity, adjudication, mint/attach doors,
  evidence quality, publication) is ranked in `claims-and-evidence.md`. A
  hub-schema change is knowledge-mesh's call.
- draft-authoring vs knowledge-mesh — knowledge-mesh ranks draft-linearization
  (the draft as a graph view); `draft-authoring.md` ranks authoring and export
  of the draft itself.
- `backlog/quest-graph-as-dossier.md` — graph-memory-consumers Horizon 1;
  supersedes the dossier-as-draft designs (quest-dossier-dialectic and
  kin). roadmap-quest Horizon 5 points at it; draft-authoring keeps
  paper-writing-pipeline rungs 7–8; knowledge-mesh owns the mesh it lives in.
- `graph-memory-consumers.md` Do-next 1 is `backlog/memory-native-authoring.md`
  (Reto 2026-10-01, top priority, depends on `backlog/file-mirror.md`); its
  search cluster is retrieval as the consumer side of navigation.

Waits:
- nanobuds Horizon 2 (approve/sign pass) sits in Reto's nanopub queue
  (td345830–td345836) with six other batches.

## platform and operations — split, deploy, monitors, agent surface

Threads: `plugin-split.md` · `monitors-that-go-quiet.md` · `factory.md`
(also ranks plan_tick health, the todo planner: plan-tick-health,
plan-tick-context-cut; agent execution lanes, budget and crash recovery) ·
`ship-gate-ci.md` (gate, CI lanes and worktree reaper) ·
`deploy-fleet-ops.md` (deploy script and fleet residue) ·
`security-hardening.md` (credentials, role and sandbox boundaries) ·
`agent-surface.md` (what an MCP call says and refuses; created 2026-10-17) ·
`context-navigation.md` (what an agent's context carries: the twin, sets,
row grammar, Mode B; created 2026-10-30).
Monitors, ship-gate-ci, deploy-fleet-ops and security-hardening serve the
operations pillar; plugin-split, factory, agent-surface and
context-navigation the platform bucket. Code-debt, db-schema and docs-audit items stay unthreaded.

Seams:
- `context-navigation` ↔ `agent-surface` — `backlog/block-navigation.md`
  (authoring lint and block sizes stay agent-surface's; lists, sets and the
  row grammar are context-navigation §3–§5) and seen-stubs
  (`backlog/context-navigation.md` §7, listed in agent-surface Horizon 1).
- `context-navigation` ↔ `graph-memory-consumers` — fisheye and the Mode B
  loop (`backlog/context-navigation.md` §7–§8); graph-memory-consumers
  lists them as a seam only.
- `context-navigation` ↔ `factory` — plan_tick is Mode B's first adopter
  (`backlog/plan-tick-context-cut.md` is the cut it generalises).
- `context-navigation` ↔ `ship-gate-ci` — `backlog/compact-handoff.md`
  (hook mechanics) merges into the twin reader once it exists.
- `ship-gate-ci` ↔ `monitors-that-go-quiet` —
  monitors owns signals that lie, ship-gate-ci owns the gate and reaper that
  produce them.
- `deploy-fleet-ops` ↔ `session-mcp-shared-server` — both touch
  `scripts/deploy` and the ensure script (`backlog/mcps-venv-deploy-gaps.md`);
  shared-server owns the MCP's deploy shape, deploy-fleet-ops the script.
  `ship-gate-ci` Do-next 4 and `deploy-fleet-ops` share the live render tree.
- `security-hardening` ↔ `session-mcp-shared-server` —
  `backlog/db-role-enforce-pgbouncer.md` and the shared-server's role
  isolation (Horizon 1) are one boundary.
- `ship-gate-ci` ↔ `ingest-and-fetch` ↔ `local-compute` — two primitives,
  one resolver (Reto 2026-10-23, shipped 2026-10-23): every outbound rule
  is a rate bucket (`external_rate_limits`) or a slot hold
  (`resource_slots`), resolved by `utils/outbound_policy.py` — spec
  deleted, the model lives in its module docstring, the gate in
  `invariants.md` §Outbound; ingest-and-fetch keeps only the gr506756
  fetch-pass confirmation.
  `local-compute` ranks `backlog/llm-lane-scheduler.md`, the `llm-local`
  class — one allocator per box (capacity controller + class lanes;
  absorbed `llm-dispatch-feedback-controller.md`), with
  `castor-agent-lane.md`'s two lanes as classes in it. Two scheduling
  levels stay distinct: the job queue (`cluster-scheduling.md`,
  `job-scheduling-fairness.md`, factory/platform) decides which job runs;
  lanes decide which LLM call gets a slot; fillers are pull generators so
  no job is minted only to wait for a lane (seam recorded 2026-10-23).
- `scripts/deploy` — the restart-on-installed-file-change work (gr457894,
  closed) and the deploy session's render-tree lock are the same script;
  no thread owns deploy.
- `scripts/main-ci-status` / check.yml — the monitors thread gates every other thread's "is main green"
  answer — and as of 2026-09-30 it answers: the stale-verdict read,
  the main-push lane range (now starting at the last sha with a real shard
  verdict), and a ruff+mypy pre-qland lint all landed. Every thread that qlands
  now pays ~3 min it did not before, and gets told about its own lint drift
  instead of the next tree's author finding it.

Waits:
- plugin-split Horizon 2–4 waited on gr457894 (each extraction is a module
  move that re-triggers the stale-worker break); it is closed, so they
  are unblocked.
- td457903 (the castor restart) was withdrawn 2026-09-30 — the ImportError
  was a stale session MCP, gr458061's shape, not prod. Nothing here waits
  on Reto.

## ingest — acquisition and extraction fidelity

Threads: `ingest-and-fetch.md`

New 2026-10-01 on Reto's ruling (the cluster gets its own thread, triaged
before it is ranked). Single-thread programme for now; it is here rather than
folded into `knowledge` because that programme owns the layer that consumes
this pipeline, not the pipeline.

Seams:
- `local-compute.md` Parked holds the **embed-drain** half of what used to be
  one cluster. Throughput there, fidelity here; different
  code, neither sequences the other.
- `backlog/graph-gardener-value.md` slice 5 (the gardener idle filler) spends
  local capacity on this pipeline's output — a corrupt extraction makes that spend worse than
  idle, so this thread's Do-next 2 is upstream of that item's value.

Waits:
- Nothing waits on Reto. The whole thread waits on its own triage (td458898):
  six of seven ranks are provisional until it runs.

## Not covered by any programme

Recorded 2026-09-30 so the gaps are visible on the map rather than discovered
one gripe at a time. Four were listed; three are gone — the ingest cluster has
its own programme above, the job-lifecycle cluster was ranked across
`monitors-that-go-quiet` and `roadmap-quest`, and `scripts/test`'s container
moved to Python 3.13 to match the ship gate. The one below has a
decision from Reto (2026-10-01) and, as of the same day, a rank in local-compute.

- **fleet capacity is unmeasured** — gr458727 (closed; was ranked in
  `local-compute.md` Horizon 12, 2026-10-01). All 20 nursery detectors answer
  "is work stuck?"; none answers "is capacity used?". Idle GPUs beside an empty
  queue are invisible and indistinguishable from a healthy fleet, which is the
  one state Reto's local-compute goal is about. Not ranked in
  `monitors-that-go-quiet`: that thread owns signals that lie, and this is a
  signal that does not exist. It has a consumer as of 2026-09-30 —
  the local idle-capacity queue (now `backlog/graph-gardener-value.md` slice 5)
  needs the same utilisation/queue-depth number — but a consumer is not an
  owner. **Ruled 2026-10-01: the measurement is authorised and belongs to
  local-compute** — measurement only, no alerting,
  because an idle GPU is often the correct state and a detector on it would be
  noise. It leaves this list once that item ranks it.
