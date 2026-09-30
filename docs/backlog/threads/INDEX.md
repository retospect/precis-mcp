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
  `graph-memory-consumers` (dormant; owns consumers of that substrate).
- **3d-design** — `pcb` + `se` + `multiscale-design-core` (dormant) +
  `se-machine-design` (dormant) + `pcb-platform` (dormant).
- **local-compute** — `serving` (owns the MCP ceiling and the fleet) +
  `local-compute` (dormant; owns what the served capacity does).
- **personal-integration** — HELD; no programme yet.
- **platform** — `platform` (split, deploy, monitors); serves all four
  indirectly (nothing ships without it).

## Active / dormant

**Active** (has a session): `ewod-pcb` · `hexfold-toolkit` ·
`monitors-that-go-quiet` · `nanobuds-paper` · `pcb-easyeda-round-trip` ·
`plugin-split` · `roadmap-quest` · `se-3d-viewer` · `se-nucleic-chain` ·
`serving-programme` · `session-mcp-shared-server` · `knowledge-mesh`.

**Dormant** (file exists, ranked, no session — opens at the next session
restart if Reto names it): `graph-memory-consumers` ·
`multiscale-design-core` · `se-machine-design` · `local-compute` ·
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
- ewod Horizon 8 (`backlog/ewod-synthesis-protocol.md`) consumes
  se-nucleic-chain's make_steps (shipped 2026-09-30; the chain thread's
  Horizon 2 points back at it).
- pcb-platform (dormant) is behind both active pcb threads on the shared
  generator/DRC/realizer files by the same seam rule above; it does not
  reorder either.

## se — 3D modelling, chains, hexfold, machine design

Threads: `se-3d-viewer.md` · `se-nucleic-chain.md` · `hexfold-toolkit.md` ·
`se-machine-design.md` (dormant) · `multiscale-design-core.md` (dormant)

Seams:
- `backlog/se-pick-hierarchy.md` — viewer Horizon 1 (the keystone) and
  chain Do-next 2 are the same surface; the viewer owns the selection
  mechanism, the chain thread owns the residue/base-pair instance.
- precis_se atomic output — a composite corrupted by a stale process
  (gr458061; gr457995 refuted, there is no join-side bug) renders in the
  viewer as a wrong picture; hexfold's `composite_part_stolen` validate
  check now reports it, so the viewer's interest is unchanged, only the cause.
- se-machine-design (dormant) owns the design model, including the
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

## serving — the MCP and the fleet's model serving

Threads: `session-mcp-shared-server.md` · `serving-programme.md` ·
`local-compute.md` (dormant)

Seams:
- `backlog/session-mcp-http-server.md` — ranked only in the shared-server
  thread; serving-programme cross-references it.
- `backlog/mcp-concurrency-load-test.md` and the K-parallel harness — the
  serving thread's py-spy answer (its Do-next 3) decides whether the
  shared-server's multiprocess item is topology or workaround.
- served vs used — serving-programme owns the MCP ceiling, vLLM Slice 0
  go/no-go and the eval-run-spine; local-compute owns what the served
  capacity does once it exists (summarise, insert, mesh, link, categorise).
  `backlog/embedder-capacity-ownership.md` (local-compute's) is fed by
  serving-programme's py-spy/topology answers.

Waits:
- serving Horizon 2 (shared-server end state) waits on serving Horizon 1's
  process-count answer.
- `backlog/curation-gate.md` waits on eval-run-spine's verdict column
  (serving Do-next 6) and is parked on by knowledge-mesh and roadmap-quest.

## knowledge — taxonomy, quests, papers

Threads: `knowledge-mesh.md` · `roadmap-quest.md` · `nanobuds-paper.md` ·
`graph-memory-consumers.md` (dormant)

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

Waits:
- nanobuds Horizon 2 (approve/sign pass) sits in Reto's nanopub queue
  (td345830–td345836) with six other batches.

## platform — split, deploy, monitors

Threads: `plugin-split.md` · `monitors-that-go-quiet.md`

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

## Not covered by any programme

Recorded 2026-09-30 so the gaps are visible on the map rather than discovered
one gripe at a time. Each needs an owner decided, not work done.

- **ingest / fetch pipeline** — 6 gripes, no thread. `knowledge` owns the layer
  that consumes this pipeline, not the pipeline. Two are silent-corruption bugs
  (gr228652, gr228699 — μ/Greek destroyed at extraction, open since ~08-21),
  which is the worst shape in a research corpus: nothing fails, the corpus is
  quietly wrong, and embeddings/findings/cites inherit it. Ids and the
  owner-decision options in `backlog/gripe-clusters-with-no-owning-thread.md`,
  which also records where 2026-09-30's review parked it: one Parked entry in
  `local-compute.md`, a holding position rather than an owner.
- **job lifecycle / unpark** — ranked 2026-09-30, so this one is closed as a
  coverage gap: gr452203 in `monitors-that-go-quiet` Do next, the four
  fix_gripe items Parked there behind Reto's inert ruling, gr454792 in
  `roadmap-quest` Horizon. They still compound (gr456240 latches leaves for
  infra reasons, gr454792 means nobody can unlatch them, gr452203 buries the
  evidence) — that argument is in the same file, now under its Status section.
- **fleet capacity is unmeasured** — gr458727. All 20 nursery detectors answer
  "is work stuck?"; none answers "is capacity used?". Idle GPUs beside an empty
  queue are invisible and indistinguishable from a healthy fleet, which is the
  one state Reto's local-compute goal is about. Not ranked in
  `monitors-that-go-quiet`: that thread owns signals that lie, and this is a
  signal that does not exist. It has a consumer as of 2026-09-30 —
  `backlog/graph-maintenance-queue.md` needs the same utilisation/queue-depth
  number as an in-scope item and an acceptance criterion — but a consumer is
  not an owner, and that item is `status: draft`.
- **the local gate and the ship gate run different Pythons** — gr458726. The
  ship gate is 3.13-only; `scripts/test`'s container is 3.12. A full green
  `/go` is therefore not evidence about the version main is gated on, and cost
  a red main on 2026-09-30. Affects every thread that ships, which is why it is
  here rather than in one of them.
