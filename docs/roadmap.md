# Roadmap — the pillar layer

> Reto's product plan, taken in at the 2026-09-30 product-plan review
> (Reto = customer, a PM session = coordinator, every live thread owner
> polled). This page sits **above** `docs/backlog/threads/INDEX.md` (the
> programme map) and `docs/backlog/threads/*.md` (the ordering layer). It
> holds four things and nothing else: what each pillar is for, which thread
> files carry it, which threads are active, and the retirement rules.
> **Pointers and intent only** — the same rule as a thread file. An item's
> content lives in `docs/backlog/<slug>.md`; a thread's order lives in its
> thread file; the mission prose lives in `docs/mission.md`.
>
> Update at the same four moments as a thread file, plus one: when the
> active set changes (a session opens or closes on a thread).

## The two halves

Everything below is one of two kinds of work, and each item carries the
letter so the split stays visible:

- **A — real functionality.** Quests, and the design of things in support
  of quests: machines, pockets, boards, cartridges, papers.
- **B — compute and model optimisation.** Memory, model choice, skills,
  local compute: the machinery that makes A cheaper, faster and more
  trustworthy.

The plugin split (`docs/backlog/threads/plugin-split.md`) cuts the package
boundary by dependency direction, not by A/B, and the two mostly coincide:
the util package is B, the geometry package and the models are A. Two known
mismatches, both recorded there: the quest kind is A but lands in the
default kit; `se` sits inside geometry rather than as a peer plugin.

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
`term-taxonomy.md`: `backlog/knowledge-mesh.md` (Reto's own statement of
this pillar), `backlog/measures-substrate.md`, `backlog/graph-gardener.md`,
`backlog/corpus-quantitative-extraction.md`. The consumer half was entirely
unthreaded at the review and is now `graph-memory-consumers.md`. The
review's evidence that the mission had not sunk in: a paper session did its
proposal in a temp file and its write checks in SQL because the surface had
no affordance (`backlog/draft-authoring-graph-affordances.md`); the memory
skill declares precis memory a different system from the harness files
(`backlog/file-mirror.md`); fisheye raises `Unsupported` on paper and
memory, and no human graph browse existed (`backlog/fisheye-everywhere.md`,
whose browser focus page is that surface). The same day a sibling session
given the same goal filed `backlog/experiment-loop.md` (hypothesis → test
→ measure → verdict through the verbs), `backlog/draft-linearization.md`
(a draft is a render of a subgraph) and `backlog/relation-constraints.md`;
all three are ranked in `term-taxonomy.md`.

**Threads.** `term-taxonomy.md` (active — substrate, the memory half and
the surfaces) · `graph-memory-consumers.md` (dormant — agent-side
affordances). Seam: substrate vs
consumers, recorded in `threads/INDEX.md`.

**Surfaces.** Agent: an agent never leaves the graph for SQL or a temp
file to do its work. Human: from any node Reto reaches its evidence, its
measures and the quest that cites it without SQL.

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
unicycle. A "smooth" transition today is a stepped collar; continuous
curvature is on hexfold's far horizon (§28.5–28.6) and is not promised.

**Where it stands.** The geometry and assembly half is real (hierarchy
addressing, ports, the level ladder, validate findings, replayable op
lists). Three things were on no item at the review and now are: the
per-region property layer, the DNA wrap, and one assembly across scales
(`backlog/cross-scale-single-assembly.md`; microfluidic cartridges,
parked 2026-09-12, unparked to the horizon only). Pockets as defined
classes and pick-and-join as a membership query are
`backlog/class-lattice-similarity-spaces-and-laws.md` (owned by
term-taxonomy, consumed here) and `backlog/se-intent-to-realize-loop.md`.

**Threads.** Active: `hexfold-toolkit.md` · `se-3d-viewer.md` ·
`se-nucleic-chain.md` · `ewod-pcb.md` · `pcb-easyeda-round-trip.md` ·
`nanobuds-paper.md`. Dormant: `multiscale-design-core.md` ·
`se-machine-design.md` · `pcb-platform.md`.

**Surfaces.** Agent: a design reads out as a replayable op list and an
intent statement is authorable. Human: click any block and read its pose,
envelope, ports, properties, findings and load path in one panel.

## Pillar 3 — local compute (B)

**End state.** The local box works continuously on the graph — adding
summaries, inserting, meshing, linking, categorising — on local model
rungs, with frontier review as the gate, and hands curated fisheye
contexts to frontier models when a frontier call is worth it. The
local-versus-cloud share is a number.

**Where it stands.** At the review no lane routed to a local model by
default: the tier ladder was moved to cloud at every rung on 2026-08-15
(`backlog/llm-tier-ladder-cloud-cutover.md`), the DGX-pair server has been
stopped since 2026-08-23, the only local lane is chunk summarisation on
melchior, no monitor answers "is capacity idle", and
`backlog/cluster-scheduling.md` tears servers down when the backlog
drains — the inverse of this pillar, now superseded by
`backlog/graph-maintenance-queue.md`. The embedder is the current
bottleneck and had no owner (`backlog/embedder-capacity-ownership.md`).
The evaluation of the model that fits one spark
(`backlog/local-serving-eval.md`, `backlog/vllm-per-node-serving.md`
Slice 0) is running under `serving-programme.md` and is this pillar's
first pointer.

**Threads.** Active: `serving-programme.md` (what is served: the MCP
ceiling, the vLLM go/no-go, eval-run-spine) · `session-mcp-shared-server.md`
(the platform blocker every thread owner named). Dormant:
`local-compute.md` (what the served capacity does).

**Surfaces.** One console row answers busy or idle per tier
(`backlog/local-cloud-share-report.md`, `backlog/graph-maintenance-queue.md`).

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
`backlog/anki-card-quality.md`, `backlog/reading-prep-loop.md`,
`backlog/remarkable-pairing.md`, `backlog/email-kind.md`,
`backlog/voice-kind-spec.md`, `backlog/briefing-audio-flat-hourly-backoff.md`,
`backlog/slide-photo-capture-to-pres.md`, `backlog/asa-ops-residuals.md`,
`backlog/session-history-into-precis.md` (also a pillar-1 consumer),
`backlog/web-basic-auth-users.md`.

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
object; there is no UX thread.

## Active and dormant threads

A thread file exists for any thread with three or more live items
(`threads/README.md`). **Active** means a session and a worktree exist
and the owner is expected to move it. **Dormant** means ranked and filed
against, nobody works it. Opening a session on a dormant thread names
which active one it replaces, or Reto widens the set.

Active (12, Reto 2026-09-30 — "the ones we have are good"):
`ewod-pcb` · `hexfold-toolkit` · `monitors-that-go-quiet` ·
`nanobuds-paper` · `pcb-easyeda-round-trip` · `plugin-split` (prep-only
until 2026-10-16) · `roadmap-quest` · `se-3d-viewer` · `se-nucleic-chain`
· `serving-programme` · `session-mcp-shared-server` · `term-taxonomy`.

Dormant (5, created at the review, open at the next restart if named):
`graph-memory-consumers` · `multiscale-design-core` · `se-machine-design`
· `local-compute` · `pcb-platform`.

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

- 2026-09-30 — all four pillars; 10 owners polled; 20 items filed, 2
  folded into a sibling's same-day items; 67 orphan gripes relinked, 9
  closed pointers pruned; open on Reto: the mechanical sweep, the stale
  sweep worktree, which dormant threads open at the restart, td458722.
