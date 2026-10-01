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

**A's output measure is one posted preprint a month** (Reto, 2026-10-01).
A preprint is the unit because review time is outside our control. The
cadence lives in precis, not here: quest `qu459585` holds one todo per
month, each blocked by the decision it waits on, and a missed month is a
`dead-end` logbook entry with its reason. Papers are not a pillar. A paper
gets a thread only while it holds the month's slot (nanobuds-paper today);
otherwise the repo-side blockers it exposes are gripes for the thread that
owns them. `/pillar-review` reads the quest's tree each pass.

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
`knowledge-mesh.md`: `backlog/knowledge-mesh.md` (Reto's own statement of
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
all three are ranked in `knowledge-mesh.md`.

**Threads.** `knowledge-mesh.md` (active — substrate, the memory half and
the surfaces) · `graph-memory-consumers.md` (active — agent-side
affordances) · `ingest-and-fetch.md` (active, created 2026-10-01 on Reto's
ruling — acquisition and extraction fidelity, what the graph is fed; triage
before rank) · `claims-and-evidence.md` (active, created 2026-10-01 —
the taproot/nanopub defect and follow-on cluster; Do-next 1 is the
computed-pathway evidence edge the claim page drops) · `draft-authoring.md`
(active, created 2026-10-01 — ranked by what the month's preprint hits).
Seams: substrate vs consumers, pipeline vs the layer that consumes it, and
the taproot umbrella vs its defect cluster, recorded in `threads/INDEX.md`.
Top priority (Reto, 2026-10-01): `backlog/memory-native-authoring.md`, the
write half of file-mirror — without it no text-memory class can retire.

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
knowledge-mesh, consumed here) and `backlog/se-intent-to-realize-loop.md`.

**Chemistry is part of design** (Reto, 2026-10-01). Molecules and
reactions are children of the same design as the parts they move: the
flagship's azobenzene is a reaction as much as a hinge. Catalysis is
chemistry's main line today — the catpath engine (`src/precis_pathway`,
reference engine in the catpath repo) and the pathway explorer — consumed
by the catalysis quests.

**Threads.** Active: `hexfold-toolkit.md` · `se-3d-viewer.md` ·
`se-nucleic-chain.md` · `ewod-pcb.md` (also owns the general PCB items —
"pcb stuff is on the ewod worker") · `pcb-easyeda-round-trip.md` ·
`nanobuds-paper.md` · `se-machine-design.md` (the se owner, activated
2026-10-01 — it carries the property layer and the intent loop the
north-star spec needs) · `chemistry.md` (created and activated
2026-10-01, catalysis first). Dormant: `multiscale-design-core.md` ·
`pcb-platform.md`.

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
summariser model). The share itself is unmeasured: `llm_call_log` records
the routed `placement` but not where a call landed
(`backlog/local-cloud-share-report.md` Slice 1 adds the column). No monitor
answers "is capacity idle", and `backlog/cluster-scheduling.md` tears
servers down when the backlog drains — the inverse of this pillar, now
superseded by `backlog/graph-maintenance-queue.md`. The embedder is the
current bottleneck (`backlog/embedder-capacity-ownership.md`). Reto
2026-10-01: bring local compute back — summariser first
(`backlog/local-summarizer.md`), then the model that fits one spark
(`backlog/vllm-per-node-serving.md` Slice 0).

**Threads.** Active: `local-compute.md` (owns local serving: the summariser
and the single-spark model, and what they do) · `serving-programme.md` (the
MCP ceiling and the eval spine) · `session-mcp-shared-server.md` (the
platform blocker every thread owner named).

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
`backlog/anki-card-quality.md`, `backlog/reading-prep-loop.md` (back under
the hold, Reto 2026-10-01), `backlog/email-kind.md`,
`backlog/voice-kind-spec.md`, `backlog/briefing-audio-flat-hourly-backoff.md`,
`backlog/slide-photo-capture-to-pres.md`, `backlog/asa-ops-residuals.md`,
`backlog/session-history-into-precis.md` (also a pillar-1 consumer),
`backlog/asa-voice-register.md`, `backlog/cast-followups.md`,
`backlog/briefing-combine-verify.md`, `backlog/news-reddit-mastodon.md`
(a personal feed, Reto 2026-10-01), `backlog/document-timeline-index.md`
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
object; there is no UX thread.

## Where an item says which pillar it serves

Every `docs/backlog/*.md` carries `pillar:` in its front matter and the
generated `docs/backlog/INDEX.md` groups by it. Two values are buckets
rather than pillars: `quests` (A — the quest content, campaigns and the
quest-loop machinery) and `platform` (B — deploy, gate, CI, MCP server
infrastructure, monitors, refactor debt: what every pillar stands on).
`scripts/backlog-lint` enforces the set.

## Active and dormant threads

A thread file exists for any thread with three or more live items
(`threads/README.md`). **Active** means a session and a worktree exist
and the owner is expected to move it. **Dormant** means ranked and filed
against, nobody works it. Opening a session on a dormant thread names
which active one it replaces, or Reto widens the set.

Active (19; the 12 Reto kept 2026-09-30 — "the ones we have are good" —
plus seven on 2026-10-01):
`ewod-pcb` · `hexfold-toolkit` · `monitors-that-go-quiet` ·
`nanobuds-paper` · `pcb-easyeda-round-trip` · `plugin-split` (prep-only
until 2026-10-16) · `roadmap-quest` · `se-3d-viewer` · `se-nucleic-chain`
· `serving-programme` · `session-mcp-shared-server` · `knowledge-mesh` ·
`claims-and-evidence` · `se-machine-design` · `chemistry` (added
2026-10-01, Reto's rulings) · `local-compute` (added 2026-10-01) ·
`ingest-and-fetch` · `draft-authoring` · `graph-memory-consumers` (all
three 2026-10-01: "ingest must work. draft authoring must work. graph
memory we want soon").

Dormant (3, open at the next restart if named): `multiscale-design-core`
· `factory` (2026-10-01, the agent-lane items moved from pillar 3) ·
`pcb-platform` (created at the 09-30 review).

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

- 2026-10-01 — Reto's rulings, not a full pass: vocabulary tiers 2 and 3
  throughout code (td459590 done); December paper becomes a
  molecular-machines paper, boxel dr42995 unscheduled; catpath moves
  to November (not started), reality-grounding to January; ingest-and-fetch, draft-authoring and graph-memory-consumers
  active. Still open: serving-programme has no session.

- 2026-10-01 — not a full pass: the paper cadence added (quest qu459585,
  October to January scheduled), and ingest-and-fetch listed as dormant.
  Open on Reto: venue td450081, vocabulary tiers td459590, boxel td345837,
  serving-programme active or dormant.

- 2026-09-30 — all four pillars; 10 owners polled; 20 items filed, 2
  folded into a sibling's same-day items; 67 orphan gripes relinked, 9
  closed pointers pruned; open on Reto: the mechanical sweep, the stale
  sweep worktree, which dormant threads open at the restart, td458722.
