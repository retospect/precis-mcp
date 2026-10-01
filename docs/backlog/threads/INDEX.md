# Thread programmes — the coordination map

Thread files own ranking for one thread. This page groups them into
programmes so sessions that collide on code or wait on each other can see
it in one place. Pointers only: a seam is a file or item two threads both
touch, plus the sequencing rule; a wait is an item one thread ranks and
another consumes. Update it at the same four moments as a thread file.

## Pillars

Four pillars sit above the programme layer, text in `docs/roadmap.md`.
Each programme below serves one or more:

- **memory-graph** — `knowledge` (knowledge-mesh owns the substrate) +
  `graph-memory-consumers` (dormant; owns consumers of that substrate) +
  `claims-and-evidence` (identity and evidence checks of claims) +
  `draft-authoring` (dormant; drafts, export, tex layer).
- **3d-design** — `pcb` + `se` + `chemistry` + `multiscale-design-core`
  (dormant) + `se-machine-design` + `pcb-platform` (dormant).
- **local-compute** — `local-compute` (active 2026-10-01; owns local model
  serving: the summariser and the single-spark model, and what they do) +
  `serving` (owns the MCP ceiling and the eval spine).
- **personal-integration** — HELD; no programme yet.
- **platform** — `platform` (split, deploy, monitors) + `factory`
  (dormant; agent execution lanes, budget, crash recovery); serves all four
  indirectly (nothing ships without it).

## Active / dormant

**Active** (has a session): `ewod-pcb` · `hexfold-toolkit` ·
`monitors-that-go-quiet` · `nanobuds-paper` · `pcb-easyeda-round-trip` ·
`plugin-split` · `roadmap-quest` · `se-3d-viewer` · `se-nucleic-chain` ·
`serving-programme` · `session-mcp-shared-server` · `knowledge-mesh` ·
`claims-and-evidence` · `chemistry` · `se-machine-design` ·
`local-compute`.

**Dormant** (file exists, ranked, no session — opens at the next session
restart if Reto names it): `graph-memory-consumers` · `draft-authoring` ·
`multiscale-design-core` · `factory` ·
`pcb-platform`.

## pcb — design, route, fabricate, order

Threads: `ewod-pcb.md` · `pcb-easyeda-round-trip.md` · `pcb-platform.md`
(dormant, sequenced behind the other two on the generator/DRC files — see
seam below)

Seams (same files, different work — sequence, never merge):
- `src/precis/pcb/generators.py`, `drc.py`, `realize.py`/`maze.py` —
  easyeda's router item (pcb-missing-constraint-classes §E-1) goes before
  ewod's escape-layer leak and escape-corridor rewrite; ewod sequences
  behind by its own choice.
- `backlog/pcb-guided-place-route.md` — ewod owns the engine slices,
  easyeda owns slice 9 (JLCPCB ordering); neither reorders the other's.
- `backlog/pcb-missing-constraint-classes.md` — easyeda owns the router
  half now, ewod owns the HV vocabulary on its Horizon.

Waits:
- ewod Horizon 10 (`backlog/ewod-synthesis-protocol.md`) consumes
  se-nucleic-chain's make_steps (shipped 2026-09-30; the chain thread's
  Horizon 2 points back at it).
- pcb-platform (dormant) is behind both active pcb threads on the shared
  generator/DRC/realizer files by the same seam rule above; it does not
  reorder either.

## se — 3D modelling, chains, hexfold, machine design

Threads: `se-3d-viewer.md` · `se-nucleic-chain.md` · `hexfold-toolkit.md` ·
`se-machine-design.md` · `multiscale-design-core.md` (dormant)

Seams:
- `backlog/se-pick-hierarchy.md` — viewer Horizon 1 (the keystone) and
  chain Horizon 9 are the same surface; the viewer owns the selection
  mechanism and the click-time pick route, the chain thread owns the
  resolver (`precis_se/pick.py`, built) the route calls.
- precis_se atomic output — a composite corrupted by a stale process
  (gr458061; gr457995 refuted, there is no join-side bug) renders in the
  viewer as a wrong picture; hexfold's `composite_part_stolen` validate
  check now reports it, so the viewer's interest is unchanged, only the cause.
- se-machine-design (the se owner, active since 2026-10-01) owns the design model, including the
  non-geometric property layer (hydrophobic, charge, field, optical);
  se-3d-viewer owns rendering whatever that model carries. Model vs
  render, not a rank duplication.
- se-machine-design also owns `backlog/pcb-se-binding.md` and
  `backlog/pcb-argue-with-design.md` — the mm→m crossing between se and
  pcb — not ewod-pcb or pcb-platform.
- multiscale-design-core (dormant) is the substrate se-machine-design's
  model stands on (design-state-core, pattern groups, complementarity);
  se-machine-design consumes it rather than re-deriving it.

Waits:
- viewer Horizon 3–4 (reaction forces, mechanical DRC phase 2) feed
  `backlog/se-feasibility-and-cost.md`, the far end all three serve.
- nanobuds-paper's seam-topology cross-cite waits on hexfold Do-next 3
  (gr456641 + gr457997, the `EnvKey` extent fix).
- td344088 (se + hexfold paper) waits on the walker dogfood (chain
  Do-next 1) for its figure.

## chemistry — pathways, catalysis, reaction facts

Threads: `chemistry.md` (pillar 3d-design; catalysis lives here, Reto
2026-10-01). Code: `src/precis_pathway`; `../catpath` is the reference
engine.

Seams:
- `backlog/pathway-presentation-shared-module.md` — plugin-split owns it
  (its Do-next 6); chemistry's UI items (Horizon 9) consume it and start
  no second copy.
- `backlog/precis-dispatch.md` — ranked in local-compute (Horizon); chemistry's
  DFT relax is its first consumer, and its seams extract when a second
  workload lands.
- roadmap-quest owns the quest loop that dispatches and consumes
  pathways; chemistry owns the engine's health and output contract and, since
  2026-10-01, the catalysis quests' content (qu164903 ticking, the qu202467
  restart report, gr345366, gr322060).
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
  `backlog/embedder-capacity-ownership.md` (local-compute's) is fed by
  serving-programme's py-spy/topology answers.

Waits:
- serving Horizon 2 (shared-server end state) waits on serving Horizon 1's
  process-count answer.
- `backlog/curation-gate.md` waits on eval-run-spine's verdict column
  (serving Do-next 3) and is parked on by knowledge-mesh and roadmap-quest.

## knowledge — taxonomy, quests, papers

Threads: `knowledge-mesh.md` · `roadmap-quest.md` · `nanobuds-paper.md` ·
`claims-and-evidence.md` · `graph-memory-consumers.md` (dormant) ·
`draft-authoring.md` (dormant)

Seams:
- `backlog/measures-substrate.md` — knowledge-mesh Do-next 5; roadmap Horizon 4
  (meta.supply widened to measures) consumes it.
- `backlog/knowledge-mesh.md` — knowledge-mesh Horizon 3; roadmap Horizon 4 is
  its in-scope 2.
- `backlog/curation-gate.md` — both park on it; owned by serving.
- `backlog/fisheye-everywhere.md` — knowledge-mesh Do-next 4; roadmap's
  `view='tree'` and the se viewer thread both render through its ladder;
  the browser focus page it adds is the human graph-browse surface
  (docs/roadmap.md pillar 1).
- `backlog/relation-constraints.md` — knowledge-mesh Do-next 3; the quest
  `serves` cycle guard roadmap-quest lacks lands there.
- `backlog/file-mirror.md` — knowledge-mesh Do-next 7; the memory half of the
  thread (context-memory-hierarchy, session-history-into-precis) is
  ranked in `knowledge-mesh.md` from 2026-09-30.
- substrate + memory half vs agent affordances — knowledge-mesh ranks the
  substrate (knowledge-mesh, measures-substrate, graph-gardener), the
  memory half and the surfaces; `graph-memory-consumers.md` (dormant)
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

## platform — split, deploy, monitors

Threads: `plugin-split.md` · `monitors-that-go-quiet.md` · `factory.md`
(also ranks plan_tick health, the todo planner: plan-tick-health,
plan-tick-context-cut)
(dormant; agent execution lanes, budget, crash recovery — pillar platform,
created 2026-10-01)

Seams:
- `scripts/deploy` — plugin-split's gr457894 (restart on installed-file
  change) and the deploy session's render-tree lock are the same script;
  no thread owns deploy, so gr457894 is ranked in plugin-split.
- `scripts/main-ci-status` / check.yml — `backlog/main-stays-gated.md`
  (monitors thread) gates every other thread's "is main green"
  answer — and as of 2026-09-30 it answers: the stale-verdict read (gr456236),
  the main-push lane range (now starting at the last sha with a real shard
  verdict), and a ruff+mypy pre-qland lint all landed. Every thread that qlands
  now pays ~3 min it did not before, and gets told about its own lint drift
  instead of the next tree's author finding it.

Waits:
- plugin-split Horizon 2–4 wait on gr457894 (each extraction is a module
  move that re-triggers the stale-worker break).
- plugin-split Do-next 1 is now gr457894 itself; td457903 (the castor
  restart) was withdrawn 2026-09-30 — the ImportError was a stale session
  MCP, gr458061's shape, not prod. Nothing here waits on Reto.

## ingest — acquisition and extraction fidelity

Threads: `ingest-and-fetch.md`

New 2026-10-01 on Reto's ruling (the cluster gets its own thread, triaged
before it is ranked). Single-thread programme for now; it is here rather than
folded into `knowledge` because that programme owns the layer that consumes
this pipeline, not the pipeline.

Seams:
- `local-compute.md` Parked holds the **embed-drain** half of what used to be
  one cluster (gr456034, gr454865). Throughput there, fidelity here; different
  code, neither sequences the other.
- `backlog/graph-maintenance-queue.md` (local-compute) spends local capacity on
  this pipeline's output — a corrupt extraction makes that spend worse than
  idle, so this thread's Do-next 2 is upstream of that item's value.

Waits:
- Nothing waits on Reto. The whole thread waits on its own triage (td458898):
  six of seven ranks are provisional until it runs.

## Not covered by any programme

Recorded 2026-09-30 so the gaps are visible on the map rather than discovered
one gripe at a time. Four were listed; three are gone — the ingest cluster has
its own programme above, the job-lifecycle cluster was ranked across
`monitors-that-go-quiet` and `roadmap-quest`, and `scripts/test`'s container
moved to Python 3.13 (gr458726) to match the ship gate. The one below has a
decision from Reto (2026-10-01) and, as of the same day, a rank in local-compute.

- **fleet capacity is unmeasured** — gr458727 (ranked in `local-compute.md`
  Horizon 12, 2026-10-01). All 20 nursery detectors answer
  "is work stuck?"; none answers "is capacity used?". Idle GPUs beside an empty
  queue are invisible and indistinguishable from a healthy fleet, which is the
  one state Reto's local-compute goal is about. Not ranked in
  `monitors-that-go-quiet`: that thread owns signals that lie, and this is a
  signal that does not exist. It has a consumer as of 2026-09-30 —
  `backlog/graph-maintenance-queue.md` needs the same utilisation/queue-depth
  number as an in-scope item and an acceptance criterion — but a consumer is
  not an owner. **Ruled 2026-10-01: the measurement is authorised and belongs to
  whoever owns `graph-maintenance-queue.md`** — measurement only, no alerting,
  because an idle GPU is often the correct state and a detector on it would be
  noise. It leaves this list once that item ranks it.
