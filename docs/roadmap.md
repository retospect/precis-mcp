# Roadmap — the pillar layer

> Reto's product plan, taken in at the 2026-09-30 product-plan review
> (Reto = customer, a PM session = coordinator, every live thread owner
> polled). This page sits **above** `docs/backlog/threads/INDEX.md` (the
> programme map) and `docs/backlog/threads/*.md` (the ordering layer). It
> holds four things and nothing else: what each pillar is for, which thread
> files carry it, where declared activity is listed, and the retirement rules.
> **Pointers and intent only** — the same rule as a thread file. An item's
> content lives in `docs/backlog/<slug>.md`; a thread's order lives in its
> thread file; the mission prose lives in `docs/mission.md`.
>
> Update at the same four moments as a thread file, plus one: when the
> fleet roster changes.

## The two halves

Everything below is one of two kinds of work, and each item carries the
letter so the split stays visible:

- **A — real functionality.** Quests, and the design of things in support
  of quests: machines, pockets, boards, cartridges, papers.
- **B — compute and model optimisation.** Memory, model choice, skills,
  local compute: the machinery that makes A cheaper, faster and more
  trustworthy.

**A's output measure is one posted preprint a month** (Reto, 2026-10-01).
A preprint is the unit because review time is outside our control. The
cadence lives in precis, not here: quest `qu459585` holds one todo per
month, each blocked by the decision it waits on, and a missed month is a
`dead-end` logbook entry with its reason. Conferences are part of the same
output (Reto, 2026-10-02): an abstract, poster or talk drawn from a paper in
the cadence. Each call is a `cfp` ref carrying its verified deadline, hung
under `qu459585` as a todo beside the paper it draws on, and written against
with `precis-proposal-help`; a recurring search keeps the list of calls
current. A conference submission does not stand in for the month's
preprint. Papers are not a pillar, and neither are conferences. A paper
gets a thread only while it holds the month's slot (nanobuds-paper today);
otherwise the repo-side blockers it exposes are gripes for the thread that
owns them. `/pillar-review` reads the quest's tree each pass.

The plugin split (`docs/backlog/threads/plugin-split.md`) cuts the package
boundary by dependency direction, not by A/B, and the two mostly coincide:
the util package is B, the geometry package and the models are A. Two known
mismatches, both recorded there: the quest kind is A but lands in the
default kit; `se` sits inside geometry rather than as a peer plugin.

## Allocation priorities

Graph memory is the leading objective (Reto, 2026-10-04). Local compute,
Meluxina, design and research progress alongside it. Paper and catalysis
receive equal research allocation. Dependencies and live regressions rank
the next build within each programme; explicit holds still apply. Personal
integration remains held; plugin-split keeps its agreed dated sequence.

Pillars describe enduring outcomes; programmes coordinate related threads;
threads own the next build and its acceptance. Read only the owning thread
and linked spec for a task. The [programme review](backlog/parallel-programme-cycle.md)
maps current next steps and blockers; these are the durable ownership seams:

| Programme | Owning threads | Build → dogfood outcome |
|---|---|---|
| Graph memory | [Consumers](backlog/threads/graph-memory-consumers.md), [substrate](backlog/threads/knowledge-mesh.md) | Author, retrieve and reconstruct real memory through the graph; retire the corresponding file workflow. |
| Local compute | [Local compute](backlog/threads/local-compute.md) | Qualify local models on actual graph workloads; measure quality, throughput and placement. |
| Meluxina ML-potential/DFT | [Local compute](backlog/threads/local-compute.md), [chemistry](backlog/threads/chemistry.md), [hexfold](backlog/threads/hexfold-toolkit.md) | Submit, recover and collect a real batch with provenance. |
| PCB place/route/EWOD | [EWOD](backlog/threads/ewod-pcb.md), [EasyEDA round-trip](backlog/threads/pcb-easyeda-round-trip.md) | One coordinated programme, distinct boards; valid placement, routed nets and geometric DRC. Sequence changes to shared generator/DRC code. |
| Printed structural parts | [Machine design](backlog/threads/se-machine-design.md) | Manufacture a part and compare measured load response with prediction. Separate materials development remains a scope question. |
| SE print normalization | [Machine design](backlog/threads/se-machine-design.md), [viewer](backlog/threads/se-3d-viewer.md) | Correct printed scale and metadata, with checks at the size actually printed. |
| Smooth carbon surfaces + Y instrumentation | [Hexfold](backlog/threads/hexfold-toolkit.md) | Tile an authored surface, verify relaxed geometry, then integrate a typed instrumentation leg. |
| Paper + catalysis | [Nanobuds paper](backlog/threads/nanobuds-paper.md), [catalysis](backlog/threads/catalysis-selectivity.md) | Export with evidence pins; compare complete reaction networks under the existing research holds. |
| Drive hierarchy | Coordinator with the web owner; [spec](backlog/drive-filter-hierarchy.md) | A new user finds a known object through a bounded set of visible choices. |

Every programme uses the [build/release/dogfood cycle](runbooks/release-cycle.md).
Each task has its own branch and worktree; integration and release work also
use separate worktrees. Release cutoffs follow completed deployments and
useful ready work, with asynchronous dogfood tied to the deployed SHA.

## Pillar 1 — memory is the graph (B)

**End state.** Text-file memory is replaced by one graph. Precis skills and
thinking, data extracted from papers, notes on papers, findings, experiment
records, *and* the repo's own guidance (Claude Code memory files,
`CLAUDE.md`, agents, conventions) live in the same space, segregated by a
closed `SPACE:` tag axis, never by a second store. Navigation is one
fisheye graph: agents over MCP, Reto over the web.

**Doctrine.** Graph first. When the graph serves a class of memory, the
text-file version is retired, not kept in parallel (see *Retirement*
below). `docs/mission.md` §"Memory is the graph" carries the one-paragraph
statement agents read.

**Where it stands.** The substrate half is threaded and moving under
`knowledge-mesh.md`: `backlog/knowledge-mesh.md` (Reto's own statement of
this pillar), `backlog/measures-substrate.md`, `backlog/graph-gardener.md`,
`backlog/corpus-quantitative-extraction.md`. The consumer half was entirely
unthreaded at the review and is now `graph-memory-consumers.md`. The
review's evidence that the mission had not sunk in: a paper session did its
proposal in a temp file and its write checks in SQL because the surface had
no affordance (shipped 2026-10-02 as draft `view='history'`/`'proposals'`
and the landed-sha edit ack); the memory
skill declares precis memory a different system from the harness files
(`backlog/file-mirror.md`); fisheye raises `Unsupported` on paper and
memory, and no human graph browse existed (`backlog/fisheye-everywhere.md`,
whose browser focus page is that surface). The same day a sibling session
given the same goal filed `backlog/experiment-loop.md` (hypothesis → test
→ measure → verdict through the verbs), `backlog/draft-linearization.md`
(a draft is a render of a subgraph) and relation constraints (shipped
2026-10-02 as columns on `relations`); the open two are ranked in
`knowledge-mesh.md`.

**Threads.** `knowledge-mesh.md` (owns substrate, the memory half and
the surfaces) · `graph-memory-consumers.md` (owns agent-side
affordances) · `ingest-and-fetch.md` (created 2026-10-01 on Reto's
ruling — acquisition and extraction fidelity, what the graph is fed; triage
before rank) · `claims-and-evidence.md` (created 2026-10-01 —
the taproot/nanopub defect and follow-on cluster; Do-next 1 is the
computed-pathway evidence edge the claim page drops) · `draft-authoring.md`
(created 2026-10-01 — ranked by what the month's preprint hits).
Seams: substrate vs consumers, pipeline vs the layer that consumes it, and
the taproot umbrella vs its defect cluster, recorded in `threads/INDEX.md`.
Top priority (Reto, 2026-10-07, "cut over soon"; supersedes the 10-01 line
and the coexistence reading of R17): the cutover sequence —
`backlog/memory-recall-walk-keep.md` slices 1a+2 → deploy the mirror
(`backlog/memory-file-mirror.md`) → refresh the 146 stale `SPACE:repo-dev`
nodes in place + the real import → hook on, `MEMORY.md` becomes a pointer
to the mesh root memory node (plus how to bring the server back up) →
`backlog/memory-native-authoring.md` as the primary write path → skills
into the mesh. One write path (the graph); files are derived. Before the
re-cut, record why the 10-03 cutover reverted.

**Surfaces.** Agent: an agent never leaves the graph for SQL or a temp
file to do its work. Human: a curated view — from a node Reto reaches its
evidence, its measures and the quest that cites it without SQL; chosen and
bounded, not an exhaustive walk (Reto, 2026-10-01).

## Pillar 2 — 3D machine design (A)

**End state.** A support structure for an LLM to reason hierarchically
about space, motion, assembly, charge, field and light, and to design
machines: state an intent ("here be pocket, this side hydrophobic, then
negative, then positive, 1 nm × 0.2 nm, backbone on the back"), then pick
and join groups until the checks pass. Nanoscale (sp² carbon, DNA
scaffolding, pockets with patterned regions) and macroscale (PCBs,
microfluidic cartridges, motors, mechanics) are children of one design.

**North-star specs.** `backlog/multiscale-design-system-spec.md` (Reto's
system spec) with `backlog/multiscale-design-architecture.md` as the map;
`backlog/se-kind.md`; `backlog/se-region-property-layer.md` (the
non-geometric layer, decided 2026-09-30).

**Named test pieces**, in build order: the **box** and the **rotary
ratchet valve** (`hexfold` spec §28); the **T-handle bearing**
(`backlog/hexfold-t-handle-bearing.md`, third piece, Reto 2026-09-30); the
**torus with an instrumentation leg**
(`backlog/hexfold-instrumentation-leg.md`); **DNA scaffold around a carbon
part** (`backlog/se-chain-wrap-around-part.md`); the cross-scale flagship,
a bistable azobenzene driving a folding-chair tensegrity switched by two
wavelengths (spec Addendum A1); the running mechanical example, the
unicycle. Authored smooth surfaces and their carbon tiling are tracked by
`backlog/hexfold-ideal-surface-then-tile.md`; instrumentation integration
remains `backlog/hexfold-instrumentation-leg.md`. A generated surface alone
does not establish relaxed carbon stability.

**Where it stands.** The geometry and assembly half is real (hierarchy
addressing, ports, the level ladder, validate findings, replayable op
lists). Three things were on no item at the review and now are: the
per-region property layer, the DNA wrap, and one assembly across scales
(`backlog/cross-scale-single-assembly.md`; microfluidic cartridges,
parked 2026-09-12, unparked to the horizon only). Pockets as defined
classes and pick-and-join as a membership query are
`backlog/class-lattice-similarity-spaces-and-laws.md` (owned by
knowledge-mesh, consumed here) and `backlog/se-intent-to-realize-loop.md`.

**Chemistry is part of design** (Reto, 2026-10-01). Molecules and
reactions are children of the same design as the parts they move: the
flagship's azobenzene is a reaction as much as a hinge. Catalysis is
chemistry's main line today — the catpath engine (`src/precis_pathway`,
reference engine in the catpath repo) and the pathway explorer — consumed
by the catalysis quests.

**Threads.** `catalysis-selectivity.md` (created and activated
2026-10-02, ranked high by Reto: NO→NH₃ must win every fork,
thermodynamically and kinetically, over a U/pH window, on a complete
network; plus the explorer's one-step-per-change diagram) ·
`hexfold-toolkit.md` · `se-3d-viewer.md` ·
`se-nucleic-chain.md` · `ewod-pcb.md` (also owns the general PCB items —
"pcb stuff is on the ewod worker") · `pcb-easyeda-round-trip.md` ·
`nanobuds-paper.md` · `se-machine-design.md` (the se owner, activated
2026-10-01 — it carries the property layer and the intent loop the
north-star spec needs) · `chemistry.md` (created and activated
2026-10-01, catalysis first). Additional ranked threads: `multiscale-design-core.md` ·
`pcb-platform.md` · `nanoreactor.md` (dormant, 2026-10-04: the
instrumented NO→NH₃ carbon nanoreactor and its T0–T6 design toolchain).

**Surfaces.** Agent: a design reads out as a replayable op list and an
intent statement is authorable. Human: click any block and read its pose,
envelope, ports, properties, findings and load path in one panel.

## Pillar 3 — local compute (B)

**End state.** The local box works continuously on the graph — adding
summaries, inserting, meshing, linking, categorising — on local model
rungs, with frontier review as the gate, and hands curated fisheye
contexts to frontier models when a frontier call is worth it. The
local-versus-cloud share is a number.

**Where it stands.** 0% of LLM traffic is local: 406,527 calls in the 7
days to 2026-10-01 all went cloud (~$298). The tier ladder moved to cloud at
every rung on 2026-08-15; the castor/pollux/spark LLM servers have been
stopped since 2026-08-23, GPUs idle, DeepSeek-V4-Flash and Qwen3 weights
staged on castor; the last local call was 2026-09-10 (the melchior
summariser model). The share is measured by `get(kind='llm',
id='/placement')`: landed (`placement`) and routed (`placement_routed`,
migration 0179, recorded from the round-1 deploy on). No monitor
answers "is capacity idle", and `backlog/cluster-scheduling.md` tears
servers down when the backlog drains — the inverse of this pillar, now
superseded by `backlog/graph-maintenance-queue.md`. The embedder is the
current bottleneck (`backlog/embedder-capacity-ownership.md`). Reto
2026-10-01: bring local compute back — summariser first
(`backlog/local-summarizer.md`), then the model that fits one spark
(`backlog/vllm-per-node-serving.md` Slice 0).

**Compute reserve: Meluxina** (Reto, 2026-10-02: "we should use compute
there"). An external Slurm HPC allocation. Two uses, coordinated by
`local-compute.md`: LLM operations through `slullama`, as
a placement-chain rung (`backlog/slullama-hpc-placement.md`: the card is
shipped dark, the rung waits on the tunnel key being registered with the
provider; td345845 as written is about melchior reaching castor/pollux,
not the Meluxina login); and catpath
runs and ML-potential/DFT batches, with chemistry and hexfold as consumers.
Batch stage/submit/poll/fetch is a separate runner from LLM tunnel serving.
The batch path is further along than the LLM path: a Codex-owned branch
(`work/meluxina/bootstrap`, paused handoff td471801) holds a generic
SSH/Slurm stage/submit/recover/collect runner that authenticated and read
quota on 2026-10-05. That read sets the design constraint: the project
space is about 97% full by bytes and by inodes, so the only footprint that
fits is one Apptainer image per workload on project space, every per-job
write on the node-local scratch, results pulled back and the stage
deleted. The next integration slice needs its own spec once a local GPAW
relax is verified (see the [programme review](backlog/parallel-programme-cycle.md)).
How it shows in the local-versus-cloud share
(its own row, or folded into local) is undecided. No hostname, address or
account id for it goes in this repo; coordinates live in the gitignored
overlay.

**Threads.** `local-compute.md` (owns local serving: the summariser
and the single-spark model, and what they do) · `session-mcp-shared-server.md`
(the platform blocker every thread owner named; since 2026-10-01 the shared
server is supervised, serves the deployed sha and reads secrets from files —
open: a container recreate strands interactive sessions, embedder capacity,
per-session DB roles). Additional ranked threads:
`serving-programme.md` (the MCP ceiling and the eval spine; Reto 2026-10-01 —
nothing hits the ~28 calls/s ceiling at ~15 sessions).

**Surfaces.** One console row answers busy or idle per tier
(`get(kind='llm', id='/placement')` for the share;
`backlog/graph-maintenance-queue.md` for busy-or-idle).

## Pillar 4 — personal integration (A, HELD)

**End state.** The human context window: Anki, reMarkable, email,
calendar. Not started, by decision (Reto 2026-09-30): user privacy and
per-user management come first.

**Gate**, both before any personal data enters the graph:
`backlog/identity-and-access.md` + `backlog/per-user-library-link.md`
(who is this row for), and `backlog/content-sensitivity-placement.md`
(what may never reach a cloud model). The `SPACE:` axis pillar 1 designs
must carry a per-user value from the start, or this pillar redoes it.

**Parked under it** (files stay; no thread, no ranking):
`backlog/anki-card-quality.md`, `backlog/reading-prep-loop.md` (back under
the hold, Reto 2026-10-01), `backlog/email-kind.md`,
`backlog/voice-kind-spec.md`, `backlog/briefing-audio-flat-hourly-backoff.md`,
`backlog/slide-photo-capture-to-pres.md`, `backlog/asa-ops-residuals.md`,
`backlog/session-history-into-precis.md` (also a pillar-1 consumer),
`backlog/asa-voice-register.md`, `backlog/cast-followups.md`,
`backlog/briefing-combine-verify.md`, `backlog/document-timeline-index.md`
(its own non-graph index; placed when the pillar opens).
Moved out 2026-10-01: web-basic-auth-users → platform, chem-name-lookup-verb
→ chemistry, ms-teams-paper-feed → ingest-and-fetch; remarkable-pairing
closed (shipped dark; pairing is Reto's).

## Surfaces — the perpendicular axis

User experience is not a fifth pillar; it is the consumer half of each
pillar, with two surfaces (agents over MCP, Reto over the web), judged per
pillar by the surface lines above. Shared invariants:

- One graph, one address scheme; MCP and web render the same object the
  same way (`backlog/universal-short-codes.md`, `backlog/unified-item-view`
  history).
- A capability is discoverable from where it is needed
  (`backlog/capability-discovery-on-a-sprawling-surface.md`).
- No silent accept: a value the system takes and then ignores is a defect
  (`backlog/pcb-keepout-does-not-bind.md` is the pattern).

Metrics: agent surface = the tool-ledger friction detector and the
measured surface error rate in `backlog/mcp-surface-economy.md`, plus the
14-day `surface-review` pass. Human surface = Reto's own friction filed as
gripes; `precis_web` has no usage instrumentation and none is planned for
one user. Surface work is ranked inside the pillar thread that owns the
object. Cross-kind Drive is a coordinated web task in
`backlog/drive-filter-hierarchy.md`; Reto approved task entry points and a
shared searchable picker. Storage/API details remain in owning-spec review.

## Where an item says which pillar it serves

Every `docs/backlog/*.md` carries `pillar:` in its front matter and the
generated `docs/backlog/INDEX.md` groups by it. Two values are buckets
rather than pillars: `quests` (A — the quest content, campaigns and the
quest-loop machinery) and `platform` (B — deploy, gate, CI, MCP server
infrastructure, monitors, refactor debt: what every pillar stands on).
`scripts/backlog-lint` enforces the set.

## Active and dormant threads

A thread file exists for any thread with three or more live items
(`threads/README.md`). **Active** means the roster allocates an owner expected to move it.
**Dormant** means ranked and filed against without that allocation.
The roster declares intent; `scripts/inflight` checks live sessions and trees. Opening a session on a dormant thread names
which active one it replaces, or Reto widens the set.

Declared activity and next actions are in the [generated priority table](backlog/threads/PRIORITIES.md).
The sole roster is `.claude/fleet/threads.tsv`; change it when the declared
active set changes. Run `python3 scripts/docs-index` if the table is missing
or stale. Live session/worktree state is checked separately with `scripts/inflight`.

## Retirement

Two rules, both "when implemented", both tested:

1. **Threads retire on their end state.** `## Do next` empty and
   `## Horizon` `(none)` retires the file (`threads/README.md`); this page
   drops it from the active set in the same commit.
2. **Text memory retires when the graph serves it.** Each file class has a
   retirement condition, held in `backlog/file-mirror.md` §"Pillar-review
   deltas":
   the harness memory index, the skill listing, the repo conventions.
   Parallel copies are the failure mode this pillar exists to end; a
   design transferred from another assistant's memory into a backlog
   item retires the copy there (first instance:
   `backlog/class-lattice-similarity-spaces-and-laws.md`,
   `backlog/materials-molecular-substitution-db.md`).

## Review cadence

Re-read this page at every session restart wave and at the four thread
moments. A pillar whose end state has not moved in a quarter is either
mis-ranked or mis-stated; say which in its "Where it stands" line rather
than leaving it.

## Review log

Newest first; one line per pass (`/pillar-review` writes it).

- 2026-10-03 — not a full pass: `ship-gate-ci` activated (Reto: "push
  should not break build"); main's CI runs cancelled each other at ~8
  qlands/hour, the drift guard was blind, and the round's local gate held
  the ship lock — `backlog/release-candidate-verdicts.md` is its p1, after
  an independent critique replaced a release-branch design with
  "every main sha gets a verdict; deploy the newest green one".
- 2026-10-03 — not a full pass: follow-up to the priority review (Reto):
  `plugin-split` is not open-ended tier-3 work — it lands its slice,
  idles until the 2026-10-16 hold expires, then resumes the module moves
  that feed the November catpath paper; recorded in the active-thread
  line above.
- 2026-10-02 — not a full pass: conferences added to A's output (Reto, via
  the review queue): `cfp` refs under qu459585, a recurring search for
  calls. Open on Reto: whether a conference submission may ever count as
  the month's output; which call fits which paper. Meluxina added to
  Pillar 3 as a compute reserve for LLM and catpath work (Reto, same
  route); open on Reto: registering the tunnel key with the provider
  (td345845 is the castor/pollux RPC key, a different item).
- 2026-10-02 — platform pass closed the 2026-10-01 pillar review: three
  dormant threads created (`ship-gate-ci`, `deploy-fleet-ops`,
  `security-hardening`) and the factory, session-mcp, chemistry, ingest,
  roadmap-quest and monitors assignments made; code-debt, db-schema and
  docs-audit left unthreaded; housekeeping deletions and the
  kind-taxonomy-audit ruling still open with Reto (td461205); the
  dark-factory-arming ruling came 2026-10-02: the fix_gripe lane is dropped.

- 2026-10-01 — Reto's rulings, not a full pass: vocabulary tiers 2 and 3
  throughout code (td459590 done); December paper becomes a
  molecular-machines paper, boxel dr42995 unscheduled; catpath moves
  to November (not started), reality-grounding to January; ingest-and-fetch, draft-authoring and graph-memory-consumers
  active; serving-programme dormant.

- 2026-10-01 — not a full pass: the paper cadence added (quest qu459585,
  October to January scheduled), and ingest-and-fetch listed as dormant.
  Open on Reto: venue td450081, vocabulary tiers td459590, boxel td345837,
  serving-programme active or dormant.

- 2026-09-30 — all four pillars; 10 owners polled; 20 items filed, 2
  folded into a sibling's same-day items; 67 orphan gripes relinked, 9
  closed pointers pruned; open on Reto: the mechanical sweep, the stale
  sweep worktree, which dormant threads open at the restart, td458722.
