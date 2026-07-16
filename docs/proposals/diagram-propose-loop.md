---
status: draft
title: The diagram-propose loop — a self-directed, source-binding drawer a document can commission
---

# The diagram-propose loop — a self-directed, source-binding drawer

> **Status: model closed 2026-07-15 (Reto + session); unbuilt.** Extends
> **ADR 0057** (element→chunk `depicts` binding + the draw-with-me turn loop) as
> its missing **slice 5**: the autonomous `diagram_propose` tick a *document*
> fires, and the retrieval-and-binding context that makes the drawer draw from
> the source instead of from the figure title. Slices 1–2 of ADR 0057 (binding
> substrate + rich turn editing) are **live**; this is the piece that lets a
> draft say *"diagram this figure"* and get a faithful, source-bound drawing
> back without a human in the canvas. Related: `draft-reader-fisheye-rail.md`
> (the fisheye context this reuses), `docs/design/reading-prep-loop.md`,
> `precis-figure-svg`.

## The incident that names the problem

Asked to draw FIG. 1 of the deck-hook patent draft — *"a perspective view …
showing the planar body, the anchor formation, the neck, and the first, second,
and third attachment hooks"* — the model drew it **wrong**: a plate with a
vertical wall, three identical candy-cane hooks, and two invented screw holes.
Not rough — topologically wrong. The anchor formation is a *splayed barbed
fluke* that seats under the deck boards; the neck is a *narrow waist*; only the
third hook is bent ~90°. All of that is written plainly in the draft.

The drawer got none of it, for one reason: **it could not read the draft.** The
turn loop (`precis/diagram/turn.py`) is a single-shot `dispatch(LlmRequest(
prompt=…))` — no tools — and its prepared context (`precis/diagram/context.py`)
inlines *only elements already bound*. With nothing bound, the model had only
the figure's title string. It guessed.

The fix is not a better prompt. It is to give the drawer the thing a human
illustrator has: **the document open in front of them, and the freedom to look
anything up.**

## What a drawer needs — the three-layer context

A drawing instruction is *"draw the wibbler inside the flurb."* To honour it the
drawer needs the corpus's actual notion of a wibbler and a flurb, plus, when the
subject is unfamiliar, the *craft* of depicting it. Three layers, in priority
order:

### Layer 1 — the owning document, collapsed (structural anchor)

A figure is **usually part of a document** (this figure *is* draft chunk
`dc2105810`). So the drawer opens with the owning draft's **collapsed tree** —
exactly what `get(kind='draft', id='deck-hook')` yields: every block as a
heading + one-line gist. Cheap, always included, and it tells the drawer where
the figure sits and what the document is about. Expansion is **on demand**: the
draft handler already exposes `extent ∈ kwd|summary|verbatim|fisheye|
fisheye+1hop` (`handlers/draft.py:270`), the same collapse-then-expand the
editor and the fisheye rail use. We never dump all 81 bodies — that is the
virtual-scroller / editor lesson.

### Layer 2 — seeded retrieval, expressed *as fisheye on Layer 1*

Parse the salient noun phrases from the instruction (`wibbler`, `flurb`) and
find where the owning document *already defines them*. **This is not a separate
flat result list — it is fisheye expansion of the relevant paragraphs inside the
Layer-1 tree.** The drawer is usually inside the draft, so "what does this doc
mean by flurb" is answered in place: the matching blocks bloom to `verbatim`
(with `fisheye` neighbours for context) right where they sit in the outline, and
everything else stays collapsed. Concretely: run each entity through `search`
scoped to the owning draft in the three modes that already exist —
`mode='semantic'`, `mode='hybrid'` (semantic + keyword), `mode='lexical'`
(verbatim exact string) — union the hit blocks, and expand those nodes. Turn 1
is grounded before the drawer spends a single tool call.

*(Rationale, per the design conversation: Layer 2 is relevant to Layer 1, and
for a drawer that is part of the draft there is no better substrate — you fisheye
the paragraphs. A free-standing figure with no owning document skips Layers 1–2
and leans on Layer 3.)*

### Layer 3 — open agentic search (find anything useful)

Beyond its own document the drawer holds the **live precis verbs** as MCP tools.
So the drawer can:

- `search(q='flurb', mode=…)` **corpus-wide** when its own document is silent;
- `get(handle, extent='verbatim')` to open any chunk it finds;
- reach **outside the corpus** through the same two verbs — the external kinds
  are just kinds: `get(kind='perplexity-research', q='how to draw a flurb in a
  patent figure')`, `search(kind='websearch' | 'wikipedia', …)` — for the
  *craft* of depiction (patent-drawing conventions, what a real fluke looks
  like) that the corpus won't hold.

**Guardrail is an admonition, not a gate.** The prompt tells the drawer: the
owning document's own words are ground truth; search the corpus to fill a
genuine gap; go external only for craft the corpus can't supply; and **bind
every element you commit to a real chunk when one exists**. We trust the model
with the freedom and watch what it does. (Per the conversation: *"I trust the
model is careful with some admonition. We'll have to see."*) **Budgets/cost caps
are explicitly out of scope for this design** — a later concern, not a gate on
the loop.

## The output is bindings, not just pixels

Whatever the drawer reads and depicts, it **binds** — element→chunk `depicts`
edges via the turn reply's `links` field (ADR 0057). That is the durable value:
the drawing joins the knowledge graph, drift is caught by the `[binding]` lint,
and the next edit arrives with the linked source in hand (`context.py`
`render_diagram_context`). A drawing produced by this loop should leave
`hook-22 → dc2105804 (transverse hook)`, `anchor-formation → dc2105800`, etc.,
behind it — the provenance the title-only guess could never have.

## The loop: single-shot → tool-using agent

`run_turn` already takes an **injected `claude_fn`** — so this is not a rewrite
of the loop, it is a second model call. The web path keeps its single-shot
`figure.turn._default_claude`; the tick injects an **agentic** `claude_fn`
(`precis/diagram/agent.py` `build_agentic_claude_fn`). The loop is fed the
three-layer context; the agent searches/expands as needed; it returns a
whole-source rewrite **plus** vocab, notes, and the `links` set, sanitized and
auto-healed exactly as today.

**Transport decision (Reto, 2026-07-15): `claude -p` + MCP tools through the
`call_claude_agent` wrapper**, reached via `dispatch(LlmRequest(tools_needed=
True, mcp_config=…))` — the same seam the structural / deep reviewers use, so
model + backend selection stays central (ADR 0046). The drawer's tool calls
therefore go over the **MCP socket** (not the in-process `runtime.dispatch`
bridge). That in-process bridge — the OSS `openai_tools` loop, no socket — is
the convergence target, deferred behind the same seam; realising it Anthropic-
natively needs a `ChatClient`-style adapter over Anthropic's native tools API.
(This corrects the earlier "no MCP socket" framing: socket now, in-process
later.)

**This deliberately breaks the `*_propose`-is-tool-less convention.** `cad_
propose` / `structure_propose` run `claude -p` with `mcp_config=None` so the
agent *physically cannot mutate* — their deliverable is a reviewable proposal a
human Applies. The drawer is the opposite: its whole value is agentic retrieval,
and a `figure` is `corpus_role='none'` — never exported, low-stakes, iterative —
so it **applies in place** (draw-with-me semantics, the turn log as the audit
trail), like the web canvas already does. A "propose then ratify" figure variant
is deferred; it is not what a document commissioning its own illustration wants.

## The tick a document fires (`diagram_propose`)

A new job_type in `src/precis/workers/job_types/diagram_propose.py`, mirroring
the `cad_propose` shape (`PARAMS_SCHEMA` / `COMPATIBLE_EXECUTORS` / `REQUIRES` /
`build_prompt` / `JobTypeSpec`) but tool-**ful** and applying:

- **Params:** `{figure_slug, instruction, owning_draft?}` (the owning draft is
  derivable from the figure's source chunk when omitted).
- **Executor / profile:** the **agent profile** on melchior (`claude_inproc`) —
  it is LLM-heavy and wants `~/.claude`. (Same SPOF as the planner; acceptable
  for a low-frequency authoring tick.)
- **Trigger:** the ordinary `dispatch` path — an open todo carrying
  `meta.executor` + these params, minted either by a human, by the planner, or
  by a first-class "commission a figure" affordance on the draft/figure (the
  affordance is a thin fast-follow; the todo route reuses everything and ships
  first).
- **Ships default-OFF** behind a flag (e.g. `PRECIS_DIAGRAM_PROPOSE_ENABLED`),
  so the slice merges dark per repo convention.
- **Idempotent / re-runnable:** each run is a turn on the same figure; the turn
  log accumulates, the SVG + bindings converge.

## Plumbing gap to close (surfaced live)

Driving the drawer from **outside** the web canvas today is half-blind: through
the exposed MCP verb schemas an agent can rewrite the raw SVG (`edit … text=`)
but **cannot** set `vocab=`/`notes=`/`viewbox=` on `edit`, nor `element=` on
`link` — those live only in the web turn loop's structured reply. The tick runs
in-process so it can call the handler directly, but for parity (and for a human
or external agent to drive the same authoring) **expose `vocab`/`notes`/`element`
through the MCP `edit`/`link` surface** (`handlers/figure.py`). Small, and it
removes the "web-canvas-only" asymmetry.

## Slices

- **Slice A — the agentic, context-rich turn loop. ✅ BUILT (unshipped).**
  `diagram/doc_context.py` assembles Layer 1 (owning-draft collapsed outline) +
  Layer 2 (entity-seeded fisheye expansion over the draft's own chunks, via the
  canonical `render_eye`); `store.figure_owning_draft` reverse-resolves the
  `has-figure` tie; `diagram/turn.py` carries `document_context` into the prompt
  (defensive — free-standing figures unchanged) with the ground-truth-first
  admonition; `diagram/agent.py` `build_agentic_claude_fn` is the tool-using
  `claude_fn` (the injected-`claude_fn` seam, not a loop rewrite) routed via the
  ADR-0046 `dispatch(tools_needed=True)` transport. Tests:
  `test_diagram_doc_context.py` (11) + `test_diagram_agent.py` (7). *Deferred
  within A:* the semantic leg of Layer 2 (today deterministic keyword/verbatim
  over the draft; corpus-wide + external retrieval is Layer 3's agentic tools).
- **Slice B — the `diagram_propose` tick. ✅ BUILT (unshipped).** The
  seeded-single-shot precursor shipped to `main` (job_type + registration +
  `claude_inproc` executor + params `{kind, ref_id, instruction, seeds?}`,
  applies in place); this slice **upgrades it in place** (reconcile = supersede)
  to inject `build_agentic_claude_fn(source=f"diagram_propose:{kind}")` into
  `run_turn`, so the tick reads/binds its own sources (L1/L2 doc context already
  flows inside `run_turn` for figures). Self-gated: agentic when
  `PRECIS_MCP_CONFIG` names a real file (auto-on wherever the agent-profile
  worker runs) or `PRECIS_DIAGRAM_AGENTIC=1`; else it degrades to the precursor's
  single-shot fn (`PRECIS_DIAGRAM_AGENTIC=0` forces that). `seeds` stay as
  optional hints (superset, no break). Tests: `test_diagram_propose.py` (gate +
  agentic-path, stubbed router). *Deferred:* mermaid L1/L2 auto-context (no
  `mermaid`-owning-draft resolver yet — mermaid runs seeds + agentic tools only).
- **Plumbing — MCP parity.** Expose `vocab`/`notes`/`element` on `edit`/`link`.

## Code map

| Concern | File |
|---|---|
| Turn loop (`document_context` wired) | `src/precis/diagram/turn.py` ✅ |
| Layer-1/Layer-2 document context | `src/precis/diagram/doc_context.py` ✅ (new) |
| Agentic tool-using `claude_fn` | `src/precis/diagram/agent.py` ✅ (new) |
| Reverse `has-figure` resolver | `src/precis/store/_draft_ops.py` `figure_owning_draft` ✅ |
| Collapse/expand extents (reuse) | `src/precis/handlers/draft.py` (`extent=`), `utils/eye_render.py` |
| Agentic transport (seam) | `dispatch(tools_needed=True)` → `utils/claude_agent.py` |
| The tick | `src/precis/workers/job_types/diagram_propose.py` ✅ (agentic `claude_fn` injected, self-gated) |
| MCP parity (vocab/notes/element) | `src/precis/handlers/figure.py` (plumbing) |
| Skill (encourage binding — already does) | `src/precis/data/skills/precis-figure-svg.md` |

## Open questions / deferred

- **Entity extraction for Layer 2:** a cheap local/haiku noun-phrase pass vs.
  letting the agent's own first searches cover it. Lean: seed with a cheap
  extractor for turn-1 grounding, let the agent search the tail.
- **Corpus-wide Layer-2 hits (outside the owning draft):** surface as a short
  "related elsewhere" list, or leave entirely to Layer 3's agentic search?
- **Propose-then-ratify figure variant** (a reviewable proposal like cad): out
  of scope; figures apply in place.
- **`mermaid` kind + `DiagramLang` beyond SVG** (ADR 0057 slices 3–4): this loop
  is generic over `DiagramLang`, so it lands for mermaid for free once that
  kind exists.
- **Budgets/cost caps:** explicitly out of scope here.
