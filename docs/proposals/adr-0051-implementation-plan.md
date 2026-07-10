---
status: draft (implementation plan — 2026-07-09)
title: ADR 0051 implementation plan — turn-taking, personas, fisheye, blackboard
---

# ADR 0051 — implementation plan

> The **design of record** is
> [ADR 0051](../decisions/0051-turn-taking-persona-threads-and-blackboard-convergence.md)
> (decisions) + [the proposal](./turn-routing-and-context-dsl.md) (reasoning +
> open issues). This doc is the **build plan**: the dependency-ordered slicing,
> what is already in the tree vs. net-new, and the detailed design of the first
> slice (**A — substrate**). Every slice ships **dark** (feature-flagged,
> default-OFF) per the repo's dark-factory discipline, so nothing lands live
> until it is deliberately switched on.

## What already exists (verified 2026-07-09)

The ADR extends a lot of live machinery rather than greenfielding it:

- **Hard prerequisite met.** ADR 0036 universal short codes are fully landed —
  all 32 persistent kinds have stable 2-char record codes + chunk codes,
  resolving via `handle_registry.{format_handle,parse,parse_relative}`
  (`src/precis/utils/handle_registry.py:56-127`), with a CI totality test
  (`tests/test_handle_registry.py`). **Nothing blocks the curation surface.**
- **The render-loop spike (§14) is green.** `plan_tick` already (a) mints a
  distinct `kind='job'` row per tick (`workers/dispatch.py:455`), (b)
  re-assembles the prompt between ticks via the ADR 0038 assembler
  (`workers/planner_prompt.py`, `_CACHED_MODULES` + `_VARIABLE_MODULES`), and
  (c) **observes tool calls live** — precis *is* the MCP server executing
  `put`/`edit`/… mid-pass, with the full stream-json retained on
  `job.meta.transcript`. Gap: it runs `--max-turns 60` (loop-until-done)
  (`workers/job_types/plan_tick.py:393`), whereas §14 wants `--max-turns 1`
  with a fisheye re-render between *every* pass. That per-pass re-render is the
  net-new part.
- **Most §6 fisheye primitives exist**: `store.resolve_relative`
  (`store/_blocks_ops.py:1198`), `view='toc'` (`utils/toc_db.py`),
  `chunk_summaries_for` (`store/_blocks_ops.py:2105`), `chunks.keywords`,
  `chunks.section_path`. The fisheye is *assembly*, not new storage.
- **Persona ingredients exist**: skills already carry `flavor: persona`
  frontmatter (`handlers/_skill_common.py:25`, `VALID_FLAVORS`), reviewer
  personas live under `data/skills/personas/`, and `planner_prompt.py` has the
  coarse `_PINNED_SKILL_ID` (`:66`) + `has_review`-gated `_m_reviewer_persona`
  (`:1352`) this generalizes.

Genuinely **net-new**:

- The **`plan` kind** (no `kind='plan'` anywhere; §15 calls it "the only
  net-new durable schema"). Modeled on `draft`.
- A **`thread_type → persona_skill_id` registry** (today thread flavor is
  inferred ad-hoc from `meta.review` + `LLM:*` tags + draft binding).
- The **eye model** (extent × persistence), decay/eviction, the 7-verb curated
  surface, `call`/`spawn`/`wait`, the logbook, the shadow router, the
  sibling-roster, variants, affinity scheduling.

## Slicing (dependency-ordered)

Each phase ships behind a flag, default-OFF, and is independently valuable.

| Phase | What | Net-new? | Depends on |
|---|---|---|---|
| **A. Substrate** | `plan` kind; `thread_type→persona` registry + persona floor; §12 injection lockdown | plan kind = yes; rest generalizes existing | 0036 ✅ |
| **B. Render-loop** | `--max-turns 1` per pass + re-render between passes (flag `PRECIS_TURN_LOOP`); per-tick `meta` working-set snapshot (eyes+cursor), append-only, re-read each turn | wraps existing plan_tick | A |
| **C. Fisheye + `focus`** | `view='fisheye'` neighborhood; eye model (extent×persistence); the **extent ladder** `kwd<summary<verbatim<fisheye<fisheye+1hop` incl. the **reference ring** (refeye); `focus(handles, level)`; decay ladder + bunched eviction + `◦`/`†` glyphs + status line; auto-eyes (recency/salience); derived-compute priority lane | assembly + eye state | B |
| **D. Logbook** | plan+ledger+cursor continuity; Haiku fact-line distiller (derived lane); mandatory per-turn log post-condition | uses plan kind | A, C |
| **E. Routing/delegate** | shadow-mode router (log triage + sampled counterfactual); `call`/`spawn`/`wait` over `requested`→job/`dispatch`; find-Call auto-dock; fan-out guards | wraps existing job substrate | B |
| **F. Blackboard** | sibling-roster query + assembler block; small-eye cross-thread orientation; artifact variants + `view='compare'` + eye-transfer | roster + variants | C, E |
| **G. Scheduling** | affinity-batched dispatch (bunch by persona/lineage); run-to-cache-break quantum | dispatcher change | B |

Ordering rationale: A is pure substrate with no unresolved-objective
dependency and unblocks B/D; B proves the §14 loop end-to-end; C is the
headline capability; D gives continuity; E/F/G layer routing, convergence, and
economics on top. The shadow router (E1) is the proposal's "lowest-risk"
learning slice but its tuning *objective* is flagged Unresolved in the ADR, so
it is not the foundation — it rides on B once there is a loop to shadow.

---

## Slice A — substrate (detailed design)

Three independent, dark-shippable pieces. None changes live planner behavior
until switched on; A1 adds a new kind that nothing dispatches to yet, A2 is a
behavior-preserving generalization, A3 hardens the subprocess boundary.

### A1 — the `plan` kind

A chunk-tree document mirroring `draft` almost 1:1, but a **distinct kind** so
it is never exported as the deliverable (§2b/§15). It is the thread's
reasoning spine: rendered *whole* every turn with `[open]`/`[wip]`/`done:`
markers + a `▸ you-are-here` cursor, store-backed so it survives tick
exhaustion.

**Mirrors `draft`** (`handlers/draft.py`), reusing the same `chunks`
columns (`handle`, `pos`, `parent_chunk_id`, `content_sha`, `retired_at`), the
same `store.reading_order` DFS, and the same relative-handle nav — so **no new
`chunks`/`chunk_events` columns are needed**.

Work items:

1. **Handle codes** — add a free record+chunk pair to
   `utils/handle_registry.py` `KIND_CODES` + `CHUNK_CODES` (verify unused at
   implementation time; `pl` is taken by plaintext — candidates: record `ln`
   / chunk `lc`, or similar). Add `"plan"` to `EXPECTED_PERSISTENT_KINDS` in
   `tests/test_handle_registry.py`.
2. **Migration 0056** (`migrations/0056_plan_kind.sql`, forward-only,
   idempotent) — `INSERT INTO kinds ('plan', FALSE, 'Plan', …) ON CONFLICT DO
   NOTHING`. Reuse existing `chunk_kinds`; no new tables/columns. Bump the
   baseline snapshot only at release time (per ADR 0031), not here.
3. **`PlanHandler`** (`handlers/plan.py`) — copy `DraftHandler`'s put/edit/
   get/search/delete/link surface, `KindSpec(kind="plan", role="artifact",
   corpus_role="none", note_like=True, views=("toc",))`. Register in
   `dispatch.py` alongside `_gated(DraftHandler)`.
4. **Whole-tree render with markers** — extend the `_render_outline` DFS
   (`draft.py:1498`) into a compact plan render: one line per node at
   `{indent}{marker} {handle} {gloss}`, where `marker ∈ {▸, [open], [wip],
   done:, ?, ⚠}` read from chunk `meta.status` / `meta.belief`, and the cursor
   `▸` read from a **model-owned** pointer on the plan ref (`meta.cursor =
   <chunk_handle>`). Cold-start fallback: first `[open]` in reading order.
5. **Promote plan-node → todo** — `put(kind='todo', anchor=lc…)` stamps
   `meta.anchor` on the minted todo (the §2b promotion; only promoted nodes arm
   dispatch, which is also the runaway-planner fix). This reuses the existing
   `TodoHandler.put` + the anchor convention; no new dispatch logic in A1.
6. **Export exclusion** — `plan` is reader-visible but must **never** be an
   export target. Add a guard in the draft-export launcher so `kind='plan'` is
   rejected as a deliverable (the corpus_role='none' + explicit export-kind
   allowlist).

Not in A1 (later phases): the *rendering it every turn into the prompt*
(that's the assembler wiring in B/D), the ledger accretion + cursor-advance
post-condition (D), the fidelity eye on the cursor (C).

**DoD:** `put`/`edit`/`get` a plan and its chunks by handle; whole-tree render
shows markers + cursor; promote mints an anchored todo; export refuses a plan;
`ruff` + `mypy` + `pytest` green in the container gate; new unit tests for the
handler + render + the handle-code totality test.

### A2 — `thread_type → persona_skill` registry + persona floor

Generalize the coarse `Profile` enum (`utils/prompt/model.py:52`) and the two
one-offs (`_PINNED_SKILL_ID`, `has_review`-gated `_m_reviewer_persona`) into a
single registry, per §2. The persona becomes the **immutable first CACHED
block** (the floor), selected by the thread's type; personas are **excluded
from the on-demand skill index**.

Work items:

1. **Registry** — a `thread_type → {persona_skill_id, known_skills[],
   extension_verbs[]}` table (a module-level dict, extensible via the same
   plugin pattern as handle codes). Seed thread types: `write-document`
   (persona = the current `precis-tasks-help` stance, or a new
   `precis-writer` persona), `review` (`precis-draft-reviewer`), `dream`
   (`dream` persona), `triage`.
2. **Thread-type resolution** — derive `thread_type` for a tick from existing
   signals (a `meta.review` set → `review`; draft-bound + `LLM:*` →
   `write-document`; `dream_agent` env → `dream`) and stash it in
   `AssemblyContext.extras['thread_type']`. Add a `thread_type` predicate
   family mirroring `has_review`.
3. **Persona floor module** — replace the fixed `_m_pinned` head of
   `_CACHED_MODULES` with `_m_persona(ctx)` that loads
   `registry[thread_type].persona_skill_id`. It stays **first** and **CACHED**
   (the floor). The pinned operational skill (`precis-tasks-help`) remains as a
   *known_skill* in the floor for `write-document`.
4. **Exclude personas from the index** — `_build_skill_index`
   (`planner_prompt.py:141`) skips `flavor == "persona"` skills (a persona is
   not a reference doc, §2).
5. **Per-persona extension-verb menu (scaffold)** — the tools table
   (`utils/prompt/tables.py`) gains a per-thread-type overlay: a base surface +
   `registry[thread_type].extension_verbs`. In A2 this only *renders* the menu
   (documentation in the prompt); actual verb gating/enforcement is C/E when
   the curated 7-verb surface lands. Kept behavior-preserving: default overlay
   = today's full listing.

**Review-as-separate-thread is deferred.** §2 says a review should be a
separately *spawned* thread (reviewer persona in the floor), not a mid-thread
VARIABLE swap. A2 introduces the registry and lets a `review` thread_type put
the reviewer persona in the floor, but does **not** yet change how review
todos are created/dispatched — the existing `has_review` VARIABLE path stays
functional so nothing regresses. Converting review into a spawned peer thread
lands with the delegation phase (E).

**DoD:** persona resolves from the registry and renders first + CACHED for each
thread type; personas absent from the skill index; existing planner + reviewer
ticks produce equivalent prompts (golden-prompt test); gate green.

### A3 — §12 injection lockdown

Guarantee a tick's rendered system prompt **equals** the assembled bytes —
nothing prepended by `claude -p`'s `CLAUDE.md` auto-discovery — while keeping
OAuth (so **not** `--bare`, which would force API-key auth).

Findings: `plan_tick` spawns the subprocess *without* `--bare` (correct, keeps
OAuth) but **inherits the daemon cwd** (typically `/`), so a project
`CLAUDE.md` in cwd or a `~/.claude/CLAUDE.md` on the agent host would be
prepended outside the assembler — a competing uncontrolled persona that also
silently mutates the "stable" cache prefix.

Work items:

1. **Neutral cwd** — run the `plan_tick` subprocess from a dedicated neutral
   working directory containing **no** `CLAUDE.md` (a per-run temp dir or a
   fixed scratch dir), set via `subprocess.run(cwd=…)` in
   `workers/job_types/plan_tick.py`. This kills *project* discovery. Workspace
   file access is already by absolute path via `PRECIS_WORKSPACE`, so a neutral
   cwd does not break file kinds.
2. **User-level guard** — a startup check that refuses/loudly-warns if
   `~/.claude/CLAUDE.md` exists on an agent host (it would be discovered
   regardless of cwd). Document the ops requirement (no `~/.claude/CLAUDE.md`
   on melchior) in `CLAUDE.md` / the deploy runbook, mirroring the existing
   agent-worker env notes.
3. **Assert** — an invariant guard that the string handed to
   `--append-system-prompt` is exactly the assembler output, logged/`log_event`
   on mismatch (defense-in-depth; the cwd + user-guard are the real fixes).

**DoD:** subprocess runs from a `CLAUDE.md`-free cwd; startup guard fires on a
stray `~/.claude/CLAUDE.md`; assert in place; gate green; a test that the
rendered system prompt round-trips unchanged.

---

## Going live — the two levels (decided 2026-07-10)

Reto's call: **ship it live, default-ON, prod is the test bed** (not a Roadmap
note). "Turn it on; it only runs after it deploys past the gate." Split into two
levels so default-ON is *safe*:

- **Level 1 — fisheye *context* (do now, all 3 sites, default-ON).** Replace/augment
  each Claude-site's hand-rolled variable layer with `render_working_set` over a
  **policy-chosen** eye set. No `focus` verb, no render-loop. ~40 lines/site,
  each behind its own flag (default-ON), and **every render wrapped so any
  failure falls back to the current context** — a bad render can never brick a
  core worker. Build **sequentially, gate-green between each** (planner → dreams
  → reviewers); one flag flips a bad site off instantly.
  - **Planner** (`workers/planner_prompt.py`, `_VARIABLE_MODULES`): add an
    `_m_fisheye` module — a `fisheye+1hop` on the anchor section gives the planner
    the **reference ring** (cited sources + linked notes) it currently lacks.
    Purely additive first (nothing removed); retire overlapping modules later if
    proven. Carries doc-gen/edit for free (drafts are authored *through* the
    planner's MCP calls — there is no separate doc-gen job).
  - **Dreams** (`workers/dream_agent.py`, builds from scratch + oracle lens):
    seed a **kind-diverse salience/recency draw** over the oracle-drawn topic —
    **memories + papers + patents** (+ web/finding), not single-kind. Patents are
    the cross-pollination dreams are *for*.
  - **Reviewers** (`workers/review.py`, all-VARIABLE assembler): eyes over the
    strategic tree — drop-in for their variable layer.
- **Level 2 — fisheye *curation* (next).** The `focus` verb on the MCP surface +
  the `--max-turns 1` render-loop (§14). The agent places its *own* eyes and the
  loop re-renders each turn. This is where two asked-for features light up:
  - **Dream self-search**: admonition ("open by searching the topic, focus the 3
    richest") **plus** system-seeded auto-eyes (inferred/transient, decay if
    unused) — a bounded mix, not exhaustive auto-expansion.
  - **Eyes on skills** (`sk<id>`): the eye *model* already holds any handle; the
    *render* needs a **flat-document renderer** for non-tree kinds (skill/memory/
    web → `kwd`=title / `summary`=gist / `verbatim`=full, no neighborhood/ring).
    A focusable, decayable manual = the working-set-native "progressive
    disclosure" (pull up `precis-citation-help` while citing, let it decay).

**Composition with skills (settled):** persona skill = the **cached** layer
(who/how, prefix-cached), fisheye working-set = the **variable** layer (what,
per-turn). The `thread_type→persona` registry (`workers/thread_persona.py`) is
the selector. One fisheye renderer × N persona skills; `PersonaSpec.extension_verbs`
lets a persona also extend the verb surface. Designed-in, not bolted-on.

---

## Slice C — the extent ladder + reference ring (detailed design)

Status: **prototyped, dark** (`working_set.Extent.HOP1`, `utils/refeye.py`,
wired into `utils/fisheye.render_fisheye`). The `focus` verb + render-loop that
*drive* it are still B/C-pending; the extent and its render are done and tested.

### C0 — the extent ladder is two axes, not one

An eye's `extent` separates *how much of the target* from *how much of the
surroundings*, monotonically:

| rung (label) | enum | shows |
|---|---|---|
| `kwd` | `TOC` | the target's keyword/bookmark line |
| `summary` | `SUMMARY` | the target's one-sentence gloss |
| `verbatim` | `FULL` | the target's full text, **alone** |
| `fisheye` | `FIDELITY` | verbatim target **+ spatial ring** (ancestors + sibling skirt / ±N span) |
| `fisheye+1hop` | `HOP1` | fisheye **+ the reference ring** (below) |

Each rung strictly contains the previous. `Extent.parse` accepts this
vocabulary (+ `1hop`/`hop1` shorthands) and the legacy identifiers; `.label`
renders the vocabulary. Decay **peels the most expensive layer first**:
a neglected `fisheye+1hop` drops to `fisheye` (ring gone, neighborhood kept)
before collapsing to `kwd`, then gone (`_decay_to`).

**Done (2026-07-09):** `summary`/`verbatim` render the node *alone*; the spatial
neighborhood appears only at `fisheye`; `kwd` is a bookmark under its ancestor
path. Each rung is now cleanly "target detail" until `fisheye`, then
"surroundings."

### C1 — the reference ring (`utils/refeye.py`)

Where the spatial fisheye walks the *reading-order* graph, the ring walks the
**reference graph** one edge out — "what does this section point at." Pure
read-time assembly over existing primitives; **no new storage, no
authoring-time edge, no migration**:

- **Outbound** (what the section's text points to) — `resolve_link_targets`
  over each chunk in the target's subtree (unions `kind:id` mentions, universal
  `[[handle]]`s, patent numbers).
- **Inbound** (what points *at* the section) — `store.links_for(ref, 'in')`
  filtered to `SEMANTIC_RELATIONS` (`related-to`/`see-also`/`supports`/`cites`…,
  **not** structural `plan-of`/`draft-of`/`parent`/`touched`). This is where
  **linked memories/notes** enter — the mentions autolinker materialises a
  `related-to` row from a note to what it cites, so "things noted on this"
  are inbound edges.

Rendered **by kind**, grouped **Cited** (papers/datasheets/patents) / **Cross-refs**
(draft/plan chunks) / **Notes** (memories/findings/…), capped per group
(`_RING_CAP=8`) with a visible `+N more — focus to expand` — no silent cap.

**Boundary: edges only.** A memory merely *about* the section but never linked
is a similarity hit — that is `search`'s job, a separate future `+recall` rung,
not a hop. Keeps the ring deterministic, zero false-positives.

**Deferred refinements:** section-scoped *inbound* edges (`links_for` projects
pos, not chunk_id, so inbound notes are draft-scoped in v1); the cited *quote*
per paper (from the `citation` ref's `source_quote`) rather than just the title;
transitive depth (emergent today — focus a ring member to expand *its* ring).

### C2 — validated (2026-07-09)

A `haiku` child, given a cited section at `fisheye+1hop`, reviewed it using the
ring with **0 tool uses**: verified both cited papers supported both claims
(by handle), read both `Notes`, derived the correct implied edits (a
thermo-vs-kinetics conflation to split; an unexplained LLZO-thickness anomaly to
add as an open problem), and flagged the second as a **gap** — a note attached
by an inbound edge that the prose never mentioned, which pure text-reading could
not have surfaced. Evidence the ring is legible to a small model and earns its
place.

---

## Cross-cutting conventions

- **Everything ships dark.** New kind dispatches to nothing until B/D wire the
  render-loop; the persona registry is behavior-preserving; the lockdown is a
  hardening. Live cutover is a later, deliberate flag flip + `/go`.
- **Forward-only migrations**; baseline regen is release-time (ADR 0031).
- **Container gate is authoritative** (`scripts/dev pytest`), not the
  torch-free host.
- **Skills are runtime docs** — a new `precis-writer`/plan persona skill is the
  agent-facing channel and must be authored alongside the code.

## Open decisions to revisit per phase

- The exact `plan` handle-code pair (pick free codes at A1 implementation).
- Whether `write-document`'s persona is the existing `precis-tasks-help` or a
  new dedicated `precis-writer` persona skill (A2).
- The neutral-cwd strategy: per-run temp vs. a fixed scratch dir (A3).
- Deferred to their phases: render-loop flag semantics (B), eye/TTL constants
  (C), the routing objective + shadow→live cutover gate (E, Unresolved in ADR).
