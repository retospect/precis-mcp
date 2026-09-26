---
status: draft
prio: normal
---

# Context memory hierarchy

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Context hierarchy — resident core, discovered DAG

_Grouped 2026-09-26; was `context-hierarchy-dag`, status draft._

Reto approved the two-level shape for conventions (2026-09-14) and then
widened it: n levels if practical, memory included, plus a recency structure,
combined with skills and the identity/"soul" layer. This item is the
resulting plan. Not started — the session that produced it shipped only these
two backlog files.

### Motivation / why

Three surfaces load into every session independently: `CLAUDE.md` (~2098 tok),
`MEMORY.md` (~3800 tok of 80 bullets), and the skill listing. They overlap —
several memory bullets restate rules already in `CLAUDE.md` or
`docs/conventions/` — and none of them has a placement rule, so each grows by
accretion.

Once a fact is *discovered* rather than preloaded, size stops being the
constraint and reachability becomes it. Both failures are silent: too big
means the signal drowns, never-found means the memory that would have
prevented the hour-long mistake was one hop away and nothing said so.

### The placement rule

> **Resident = what fires without being asked. Discovered = what answers a
> question you know you're asking.**
> Demote anything that *answers*. Keep anything that *prevents*.

`0%-CPU client is not a dead build` prevents — resident. `se kind slice
state` answers, and the prompt names `se` when it is needed — discovered.

### Layers

**L0 — soul (resident, exempt from demotion).** Identity + constraints
(`AGENTS.md` §Identity), response style (`CLAUDE.md` §Response style),
standing prohibitions (session MCP targets prod; forward-only migrations; no
cluster addresses; `safe_get` for agent-supplied URLs; no patent reading for
commercial work). These are not lookups — they shape every answer. The
exemption is stated so later diet passes do not nibble at them.

**L1 — traps + addresses (resident).** The biting one-liners of
`CLAUDE.md` §Hook/gate-enforced, stated as rules with rationale moved to the
leaf that owns it; plus ~6 address lines (conventions skill, memory DAG root,
`docs/codebase.md`, `docs/backlog/INDEX.md`, `docs/runbooks/INDEX.md`,
`AGENTS.md`). Symptom-recognition memory bullets stay here — they fire on
recognition, and a hop kills that.

**L2 — indexes (discovered).** `.claude/skills/conventions/SKILL.md`;
~5 memory cluster notes; the generated backlog and runbook indexes. Size
genuinely stops mattering at this level.

**L3 — leaves.** `docs/conventions/*.md`, the 83 memory topic files,
runbooks, package docstrings. Unchanged — leaves stay the single source of
truth; indexes never copy their content.

Edges already exist in all three dialects: `[[slug]]` in memory, markdown
links in docs, skill frontmatter in the session listing. It is a DAG, not a
tree — a leaf takes several inbound edges (photonic-arm hangs off both the
multiscale cluster and the quest cluster).

### Recency bands — computed, not curated

| band | test | placement |
|---|---|---|
| `ACTIVE` | touched < ~7d, or carries open-work words | head of its cluster, full hook |
| `SETTLED` | landed + verified, durable fact worth keeping | mid-cluster, name + one clause |
| `SPENT` | every cited sha in `main`, no open-work words | cluster tail, delete candidate next pass |

A memory moves band by oracle, never by hand. The SPENT oracle already exists
in `scripts/memory-lint` check 2 and is currently dead — see
`memory-lint-threads-scan-dead.md`. Reviving it and having it emit a band per
memory is the entire recency mechanism; no new machinery.

### Insertion rule — the anti-flattening part

The `##` grouping the lint documents *did* exist and drifted back to flat.
Structure with no enforced insertion rule rots to flat. So a new fact is
placed by:

1. Prevent or answer? → L1 trap line, or L3 leaf.
2. Which cluster already owns the subject? → link from there. If none owns
   it, it is a new cluster, or it is not a memory at all — it is a doc.
3. **New lint check:** a leaf whose only inbound edge is `MEMORY.md` is
   *unplaced* — flag it. This is the inverse of the existing unindexed check
   and is what makes step 2 stick.

### Skills

`conventions` is the first instance of **one index skill per domain**, never
one per doc. The cap is real: every skill description loads into the resident
listing, so index skills carry a resident cost even though their bodies do
not.

No memory skill. A skill is *invoked*; a memory is *recalled*. Wrapping
memories in a skill strictly reduces reachability. Cluster notes stay plain
memory files so they keep both paths — harness relevance-recall and
link-walk.

### Phases

**P0 — repo-only, one commit, gateable.**
- `.claude/skills/conventions/SKILL.md` — annotated toc over
  `docs/conventions/*.md`, one line per leaf (when to reach for it + path),
  no content copied. 14 entries, including a `rtk.md` §Long-running commands
  entry.
- `docs/conventions/rtk.md` — author §Long-running commands: background
  launch, redirect to a log, harness exit notification as the completion
  signal, optional Monitor with a filter covering every failure signature,
  and no waiter loops / blocking `TaskOutput`. Fold in the ~22K stdout-cap
  fact. This section does not exist yet; the nearest prose is
  `.claude/skills/quiet-output/SKILL.md` §"Long runs", which then collapses
  to a pointer.
- `CLAUDE.md` §Hook/gate-enforced — keep every rule, drop the rationale that
  already lives in a leaf, collapse the three `→ docs/conventions/x.md`
  arrows into one router line.
- `AGENTS.md` §On-demand pointers — same collapse for the **Conventions**
  bullet, which currently hand-picks `thresholds.md` and
  `llm-facing-prose.md`. The three inline refs at lines 92/125/130 stay:
  load-bearing inside a procedure, not a toc.
- `scripts/memory-lint` — fix check 2, add the band emitter, add the
  unplaced-leaf check.

**P1 — user-level memory files, outside the repo (cannot share the commit).**
Restore the five `##` groups; run the currency pass against the now-live band
report; retire SPENT; write the ~5 cluster notes
(multiscale, remarkable, claims, gripes, quests); rewire edges.

Known absorb candidates — memory bullets duplicating repo docs:
`background-task output cap kills gates` (becomes the rtk §Long-running
section), `no _Verified stamps` (already `llm-facing-prose.md` freshness
contract), `deploy tree: no real hostnames` (already a CLAUDE.md one-liner).
`soft-delete filters in ad-hoc SQL` and `green gate != green CI` look similar
but carry operational detail — check against the docs before killing.

**P2 — needs Reto's call.** Whether §Identity/prohibitions and §Response
style consolidate into one resident block, and whether that lives in
`CLAUDE.md` or its own `.claude/` file. Touches the most always-loaded text.

### Measurement

`scripts/memory-lint` prints the preamble token count (CLAUDE.md+MEMORY.md;
5904 tok against a 6000 budget at 2026-09-14). P0 alone is close to
token-neutral — three pointer fragments out (~35 tok), one router line in
(~38 tok), and the new skill's description enters the resident listing
(~40 tok). The shrink comes from the rationale trimming in P0 and, much more,
from P1's thread collapse: the mass is bullet *count*, not fat bullets
(75 of 80 bullets are under 300 B), so only collapsing many lines moves the
number.

### Caveats

- "Size no longer matters" holds for L2/L3 only. L0, L1 and the skill listing
  are resident by definition.
- The failure mode moves from *too big* to *never found* — equally silent.
  The band report and the unplaced-leaf check are the only automated defence;
  the rest is discipline.
- Load-bearing assumption: the harness's relevance-recall over topic files is
  good enough to make discovery work. Unverified from inside a session.

### Out of scope

`.claude/commands/*` (go/land/qland/next are task skills, not conventions);
per-convention skills — one index line in the session listing is the point.

## Agent-tag-scoped sticky memories

_Grouped 2026-09-26; was `agent-tag-scoped-sticky-memories`, status draft, prio normal._

Split out of the soul-store assessment 2026-09-16
(`soul-splay-prime-adoption.md`) as the one piece worth doing
independently.

### Problem

asa's preamble tier 3 pulls sticky (pinned) memories, keyed per *user*
via the `author_handle` tag pattern (`src/asa_bot/preamble.py`). As
more agents share the memory kind (asa Discord, asa_slack, future
personas), there is no per-*agent* scoping: every agent surfaces the
same pinned identity/behavior memories. One store should serve N
personas with distinct initial slices.

### Proposal

Scope sticky-memory retrieval by an agent tag, the same mechanism as
the existing per-user tag key:

- Each serving surface passes its agent identity; tier-3 retrieval
  filters pins to `agent:<name>` plus untagged/shared pins.
- Tag as **rerank bias, not hard gate**, or at minimum always include
  untagged pins: a hard gate makes an untagged memory invisible to
  every agent's startup, and write-time tag discipline becomes
  load-bearing. Bias degrades gracefully.
- No schema change: existing open tag axis on `memory`.

### Open questions

- Whether shared pins are the untagged set or an explicit
  `agent:shared` tag (explicit is auditable; untagged-as-shared is the
  graceful default — probably both: explicit shared tag honored,
  untagged treated as shared).
- Where the agent name enters `preamble.build(...)` — likely alongside
  `platform`, which already distinguishes Discord/Slack.

## Soul store: splay promotion + soul_prime — deferred

_Grouped 2026-09-26; was `soul-splay-prime-adoption`, status idea, prio low._

Decision session 2026-09-16 (Reto + agent), tranquil-fluttering-waffle
worktree. **Verdict: do not build now.** Revisit when the adoption
criteria below are met.

### What was proposed

A "soul store" prototyped by the agents in
<https://github.com/jordanhubbard/mac> (PR #814, open/unmerged as of
2026-09-16: `soul_graph.py` engine + `soul_mcp.py` server +
`soul_seed.py` startup gate, single PR despite the stack description;
`natasha/soul-mcp*` and `natasha/soul-prime` branches overlap it).
Three traversals over one node graph — splay (access promotes,
forgetting is structural ordering, pinned axioms at infinity), DAG
causal edges, tag cuts — plus RAG over the same nodes, and a
`soul_prime` startup tool: read the splay root + one discovery hop
seeded by session context, hard ~300-token budget, silent when context
novelty is below threshold.

### Mapping onto precis — most of it already exists

| Soul-store piece | Precis today |
|---|---|
| DAG causal edges | typed `link` between refs |
| Tag sideways cuts | tag axis on `memory` |
| RAG over the same nodes | hybrid search + embeddings |
| Pinned axioms | asa preamble tier 3: sticky memories, pinned with expiry |
| Per-agent filtering | per-user memory tag key (`author_handle` pattern) |
| Splay access-promotion | **missing** |
| Budgeted novelty-gated startup priming | **missing** |

So an adoption would port the two missing *behaviors* onto the memory
kind — a promotion score on refs plus a prime view / fifth preamble
tier — never vendor the parallel store (duplicate embeddings, duplicate
tags, two places a fact can live).

### Why deferred

1. **No demonstrated pain.** The 4-tier asa preamble
   (`src/asa_bot/preamble.py`) already varies per turn; no gripe exists
   whose root cause is context-selection failure the tiers can't
   express. Architecture-driven, not incident-driven.
2. **The static SOUL file is the fallback layer.** asa degrades to
   SOUL-only when the preamble build fails; making identity a prod-DB
   read removes the last dependency-free layer. Any adoption keeps the
   file as degraded mode, capping the upside.
3. **Access-promotion is a feedback loop with a silent failure mode.**
   Heat measures traffic, not importance; the busiest channel's themes
   dominate while a rarely-touched critical memory sinks invisibly.
   Current design routes importance through judgment (sticky pins,
   dream/consolidation pass), which is auditable; splay replaces
   judgment with usage stats.
4. **The novelty gate is unevaluable without an A/B harness.** A
   false-familiar (needed injection, gate stayed silent) surfaces in no
   log. The mac fleet is a purpose-built dogfood environment running
   this now — that evidence is free; adopting first buys the risk
   without the data.
5. **Promotion-on-access turns reads into prod writes** (agent_rw),
   against the deliberate-write convention. Solvable, but more moving
   parts for an unproven benefit.

### Adoption criteria (any two of three → revisit)

- (a) mac dogfood shows a high silence rate on familiar contexts with
  no caught false-familiar;
- (b) a handful of injections a static preamble demonstrably wouldn't
  have produced;
- (c) an asa-side gripe whose root cause is context-selection failure
  the current preamble tiers cannot express.

If adopted: key promotion heat per agent tag (one DAG, N orderings) —
shared heat across agents lets whoever ran last reshape everyone's
root. Pinning is also per agent tag: shared vs per-agent axioms are
different souls.

### Split out (independent, worth doing on its own merits)

Per-agent tag-scoped sticky memories —
`agent-tag-scoped-sticky-memories.md`. Cheap, judgment-preserving, no
new mechanics.
