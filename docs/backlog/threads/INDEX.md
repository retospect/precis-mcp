# Thread programmes — the coordination map

Thread files own ranking for one thread. This page groups them into
programmes so sessions that collide on code or wait on each other can see
it in one place. Pointers only: a seam is a file or item two threads both
touch, plus the sequencing rule; a wait is an item one thread ranks and
another consumes. Update it at the same four moments as a thread file.

## pcb — design, route, fabricate, order

Threads: `ewod-pcb.md` · `pcb-easyeda-round-trip.md`

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
- easyeda Do-next 1 (gr457053 rebase) and its whole tree wait on Reto
  allowing the ship; three of its pointers are forward-looking until then.
- ewod Horizon 8 (`backlog/ewod-synthesis-protocol.md`) consumes
  se-nucleic-chain's make_steps (its Do-next 1, slice B).

## se — 3D modelling, chains, hexfold

Threads: `se-3d-viewer.md` · `se-nucleic-chain.md` · `hexfold-toolkit.md`

Seams:
- `backlog/se-pick-hierarchy.md` — viewer Horizon 1 (the keystone) and
  chain Do-next 4 are the same surface; the viewer owns the selection
  mechanism, the chain thread owns the residue/base-pair instance.
- precis_se atomic output — hexfold's join corruption (gr457995) renders
  in the viewer as a wrong picture; the fix is hexfold's, the viewer only
  needs to know a wrong picture may be upstream data.

Waits:
- viewer Horizon 3–4 (reaction forces, mechanical DRC phase 2) feed
  `backlog/se-feasibility-and-cost.md`, the far end all three serve.
- nanobuds-paper's seam-topology cross-cite waits on hexfold Do-next 6
  (measured rows trusted).
- td344088 (se + hexfold paper) waits on the walker dogfood (chain
  Do-next 1) for its figure.

## serving — the MCP and the fleet's model serving

Threads: `session-mcp-shared-server.md` · `serving-programme.md`

Seams:
- `backlog/session-mcp-http-server.md` — ranked only in the shared-server
  thread; serving-programme cross-references it.
- `backlog/mcp-concurrency-load-test.md` and the K-parallel harness — the
  serving thread's py-spy answer (its Do-next 3) decides whether the
  shared-server's multiprocess item is topology or workaround.

Waits:
- serving Horizon 2 (shared-server end state) waits on serving Horizon 1's
  process-count answer.
- `backlog/curation-gate.md` waits on eval-run-spine's verdict column
  (serving Do-next 6) and is parked on by term-taxonomy and roadmap-quest.

## knowledge — taxonomy, quests, papers

Threads: `term-taxonomy.md` · `roadmap-quest.md` · `nanobuds-paper.md`

Seams:
- `backlog/measures-substrate.md` — taxonomy Do-next 4; roadmap Horizon 4
  (meta.supply widened to measures) consumes it.
- `backlog/knowledge-mesh.md` — taxonomy Horizon 6; roadmap Horizon 4 is
  its in-scope 2.
- `backlog/curation-gate.md` — both park on it; owned by serving.

Waits:
- nanobuds Horizon 4 (approve/sign pass) sits in Reto's nanopub queue
  (td345830–td345836) with six other batches.

## platform — split, deploy, monitors

Threads: `plugin-split.md` · `monitors-that-go-quiet.md`

Seams:
- `scripts/deploy` — plugin-split's gr457894 (restart on installed-file
  change) and the deploy session's render-tree lock are the same script;
  no thread owns deploy, so gr457894 is ranked in plugin-split.
- `scripts/main-ci-status` / check.yml — monitors Do-next 1
  (`backlog/main-stays-gated.md`) gates every other thread's "is main green"
  answer. Two halves landed 2026-09-30: the stale-verdict read (gr456236) and
  the main-push lane range, which now starts at the last sha with a real
  shard verdict. What remains is qland shipping unlinted, unchecked code in
  the first place — every thread that qlands is upstream of it.

Waits:
- plugin-split Horizon 2–4 wait on gr457894 (each extraction is a module
  move that re-triggers the stale-worker break).
- plugin-split Do-next 1 (td457903) is a Reto restart, not code.
