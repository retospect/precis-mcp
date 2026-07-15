# Rich diagram editing with element→chunk binding (design-of-record)

> Design-of-record for two coupled moves: (1) a **`mermaid` kind** that
> mirrors `figure`, and (2) a **chunk-binding substrate** that lets an
> element inside a diagram (an SVG shape, a mermaid node) link to another
> chunk — a draft `dc…` chunk, a paper chunk, a memory — so the LLM edits
> the diagram inside a *prepared context* that lists every element, its
> coordinates/topology, and the linked chunk's text. Decisions are recorded
> in [ADR 0057](../decisions/0057-diagram-chunk-binding-and-mermaid.md);
> this file is the full picture the slices reference. Keep it true —
> update it in the commit that changes what it describes.

## Why

Today `figure` (an SVG canvas the model draws *with* the human) is a closed
world: the drawing knows nothing about the corpus it depicts. But a figure of
a deck hook, or a mermaid flow of a pipeline, *is about things precis already
holds* — a `draft` part description, a `cad` cross-section, a paper's method,
a memory. If each meaningful element of the diagram is **bound to the chunk
that describes it**, two things become possible:

1. **The model reasons about the drawing with the source in hand.** When it
   edits, it sees "this circle is `hook-latch`, bound to `dc418` — *here is
   dc418's text*." It can check the drawing against the description and fix
   drift, instead of hallucinating geometry.
2. **The diagram joins the knowledge graph.** "What depicts `memory:184`?"
   and "which figures reference this part?" become graph queries, because the
   binding is a real typed link, not a comment in the markup.

This is the diagram analogue of how the **draft/plan editor already works**:
the planner surfaces a chunk's text plus its linked context and edits in that
frame (`planner_prompt._render_anchor_context`). We generalize that from *one
anchor chunk* to *every element of a diagram*.

## What already exists (three lucky pieces)

Most of the substrate is already in the tree; the new work is wiring, not
green-field.

1. **The draw-with-me turn loop** — `src/precis/figure/turn.py`.
   `build_prompt()` (≈L162–216) already assembles: pinned skill body → canvas
   viewBox → shared vocab → private notes → current source → lints → user
   message → a JSON contract. `_ask_with_heal()` does a **bounded auto-heal**
   that never lets a broken reply overwrite a good source. This is the exact
   shape both figure and mermaid want; it becomes the shared core.

2. **Chunk→chunk links are already in the schema.** `links` carries
   `src_chunk_id` / `dst_chunk_id` (nullable — NULL ⇒ ref-level), a `relation`
   string, and a `meta jsonb`. `store.chunk_connections(ref_id, handles)`
   already walks that graph for a set of chunk handles and resolves each edge
   to the *other* ref (kind / ident / relation / title). Today only ref-level
   links are *exposed by handlers*; the chunk-level columns are unused but
   present. We light them up.

3. **The planner already surfaces linked-chunk context.**
   `planner_prompt._render_anchor_context()` (≈L914–971) takes one chunk,
   quotes its text, then lists `chunk_connections()` as "Linked to this chunk
   (use as context / sources): `- memory:184 (derived-from) — …`". That is the
   payload we want, at single-chunk granularity. The diagram context
   assembler is this generalized to a whole element set.

Universal handles (ADR 0036) give every chunk a stable address —
`dc<id>` (draft), `pc<id>` (paper), `me<id>` (memory), etc. — minted from the
immutable `chunks.chunk_id`, so a binding target never dangles under edits.

## Decision 1 — how an element binds to a chunk (hybrid)

An element's stable `id=` in the source is the **join key**; a chunk→chunk
**link row** is the semantic edge, with the element id in `link.meta`.

- The diagram source already carries stable ids on meaningful elements —
  `precis-figure-svg` mandates `id=` + `<title>` on SVG shapes, and mermaid
  nodes are named by construction (`A[Start]`, `A --> B`).
- The binding is a link row: `src` = the diagram's source chunk
  (`figure_node` / `mermaid_node`), `dst` = the target chunk, `relation =
  'depicts'`, `meta = {"element": "hook-latch"}`.

Why hybrid over the alternatives (both rejected, see below): it needs **zero
sanitizer change** (no new source attributes to allow/strip), it's
**reverse-queryable** through the same `chunk_connections()` the planner uses,
and a **dangling-binding lint** (a `meta.element` with no matching id in the
source) catches drift — one more `LintFinding` kind beside compile/bounds.

It is the element-granular cousin of the existing `plots` / `plotted-by`
relation ("a figure chunk renders a data chunk", ADR 0035); `depicts` is the
same idea at element rather than whole-figure grain.

### The relation

New pair `depicts` / `depicted-in`, chunk-level, seeded by migration into the
`relations` table. No core literal edit is needed: per ADR 0056 the relation
vocabulary is **open** — `validate_relation(store=…)` reads the live
`relations` table — so a migration insert is the whole registration.

## Decision 2 — the prepared-context payload

A new assembler `render_diagram_context(store, ref)` produces, on top of the
existing figure prompt layers, the block the whole feature exists for:

```
## Diagram elements ↔ linked context
[{element, shape, coords, chunk, relation, title}]:
  deckhook     rect    x40 y60 w80 h20    dc412  depicts  "Deck hook (304 SS stamping)"
  hook-latch   circle  cx120 cy70 r8      dc418  depicts  "Latch pawl"
  load-arrow   line    40,60→120,70       me184  depicts  "why the load path matters"
  (unbound: bracket-plate — no linked chunk)

## Linked chunk bodies
### dc412 — draft:sailboat — "Deck hook"
> The deck hook is a stamped 304 stainless part that captures the boom…
### dc418 — draft:sailboat — "Latch pawl"
> …
### me184 — memory — "load path"
> …
```

- **Coordinates come free on the SVG side** — `figure/svg.py` already computes
  element bounding boxes for the out-of-bounds lint (`_shape_bbox`); the
  assembler reuses it.
- **Mermaid has no author-time coordinates** (layout is automatic), so "coords"
  degrades gracefully to **topology**: node id + its edges. State this plainly
  in the skill — the coordinate view is SVG-rich, mermaid-topological.
- The block plugs into `build_prompt()` right after "Current source", and is
  **also returned by `get(kind='figure'|'mermaid', id=…)`**, so an external MCP
  agent editing over the wire sees the identical prepared context. One
  assembler, two callers (the turn loop and the read verb).

## Decision 3 — the turn grows a `links` field

So the model can create/adjust bindings *as it draws*, the turn's JSON
contract gains a `links` array beside `svg`/`vocab`/`notes`:

```json
{"reply":"…","svg":"…","vocab":"…","notes":"…",
 "links":[{"element":"deckhook","target":"dc412","relation":"depicts"},
          {"element":"hook-latch","target":"dc418","relation":"depicts"}]}
```

Persistence order is unchanged for source/vocab/notes (sanitize-gated, never
overwrite a good source with a broken reply); then the bindings are
**reconciled** — create new rows, drop removed ones — as chunk-links from the
source chunk. Omitting `links` leaves bindings untouched (so a chat-only or
geometry-only turn doesn't disturb them).

## How a tick builds it — the two driving scenarios

Both are the planner-context pattern (`planner_prompt` two-layer prompt: a
cached system layer + a per-tick variable layer), specialized to a diagram.

**Scenario A — build from scratch.** *"Make a diagram of the deck hook; here's
another view of it, a CAD cross-section, and 5 chunks describing things."* The
tick is handed a **seed set** of source handles — the other view (a `figure`
ref), the cross-section (a `cad` analysis chunk), the descriptive `dc`/paper/
memory chunks — rendered as reading material, exactly like the planner's
`_render_seeds`. The model drafts the source **and emits the bindings in the
same reply** (the `links` field), binding `id="deckhook"` → `dc412` as it
draws. First tick: an unbound drawing becomes a bound one.

**Scenario B — verify as it stands.** *"Here's the diagram; each line/circle is
linked to its chunk, which is also shown; ensure it's right and fix it."* Same
assembler, now populated — the model gets elements + coords + linked-chunk
bodies and checks the drawing against the descriptions, returning a corrected
source and adjusted links. For mermaid this is the flow-diagram case verbatim:
the overall-flow paragraph linked at ref level, each node linked to its phase
chunk, and a **coverage lint** asserting *node set == linked-phase set* (every
phase depicted, no orphan node).

The autonomous form of both is a `diagram_propose` job (slice 5) on the
derived-job lane, minted under a todo/project the way `plan_tick` is.

## Decision 4 — factor the shared core; figure and mermaid are instances

Figure and mermaid share the turn loop, the three-doc model (source / shared
vocab / private notes), the turn log, the element→chunk bindings, and the
context assembler. They differ only in the **source language**: compile,
sanitize, lint, extract-elements, render. That is a strategy port — the same
shape as the kind-parameterized `DraftMixin`.

Pull the shared machinery into `src/precis/diagram/` behind a small
`DiagramLang` interface:

```python
class DiagramLang(Protocol):
    name: str                                   # "svg" | "mermaid"
    def compile_error(self, src: str) -> str | None: ...      # None if valid
    def sanitize(self, src: str) -> str: ...                  # safe to inline
    def lint(self, src: str, *, viewbox=None) -> list[LintFinding]: ...
    def elements(self, src: str) -> list[Element]: ...        # id, shape, coords/edges
    def render_inline(self, src: str) -> str: ...             # SVG string for <img>
```

- **`figure` = the SVG instance.** `figure/svg.py` already *is* this — its
  `parse_error`, `sanitize_svg`, `lint_svg`, `_shape_bbox` map onto the port
  one-to-one. The refactor is mechanical and parity-guarded (the
  `tests/test_cad_parity.py` pattern).
- **`mermaid` = the mermaid instance**, below.

`figure`'s existing on-disk kind, handles, and migrations are untouched — it
becomes an *instance of* the shared core, not a copy source for mermaid.

## Decision 5 — mermaid validation/render/export is pure-Python (`mermaidx`)

**No container, no Node, no browser.** The `mermaidx` PyPI package
(successor to `mmdc`; MIT) runs the real mermaid.js inside an embedded
QuickJS engine (`quickjs-ng`) and rasterizes with `resvg-py` (Rust). Verified
on 2026-07-15:

- `render(src).svg()` → 13–22 KB SVG, **~0.2 s cold / ~0.02 s warm**.
- Invalid source raises `RuntimeError` carrying the **actual mermaid parse
  error with line number and caret** (`Parse error on line 3: … ----^
  Expecting 'SQE', 'TAGEND'…`) — ideal heal-loop feedback.
- Gibberish/empty → `UnknownDiagramError`.
- Wheels cover the whole fleet with **no compiler needed**: `mermaidx` +
  `termaid` are pure-python; `quickjs-ng` + `resvg-py` ship macOS-arm64
  (melchior/caspar) + manylinux/musllinux x86_64/aarch64 (spark).

So the mermaid `DiagramLang` is nearly free:

- `compile_error` = `render(src).svg()` under `try/except RuntimeError`,
  returning the mermaid error string (with its line/caret) or `None`.
- `render_inline` = the same SVG.
- `sanitize` = **reuse figure's `sanitize_svg`** on that SVG (mermaidx output
  is SVG), so the display path and trust boundary are shared.
- `elements` = a small source scan for node declarations + edges (the one
  bespoke piece; not validation). Node ids are the *authoring* ids the model
  binds to (`A`, `hook-latch`), stable and human-facing — preferred over the
  decorated ids mermaid emits into the rendered SVG.
- Export (PNG/PDF/standalone-SVG) = `.png()` / `.pdf()` / `.svg()`.

`mermaidx` goes behind a **`[mermaid]` extra + lazy import**, gated by
`PRECIS_MERMAID_ENABLED` (default OFF), so the base/slim-embedder venvs are
untouched and the kind ships dark — same posture as `anki`, `classify`, the
sandbox executor. Behind the `DiagramLang` port, swapping the engine later
(for a browser round-trip, or the rejected container) is a one-file change.

**Caveats (recorded, not blockers):** single maintainer, already renamed once
(mmdc→mermaidx) — mitigated by MIT/forkable + isolation behind the extra and
the port; the bundled mermaid version tracks `termaid`, so new diagram types
lag upstream until it bumps; layout uses approximated font metrics (no real
browser DOM) so text placement isn't pixel-identical — irrelevant for
validation + a legible embedded diagram, and client-side mermaid.js remains a
fidelity fallback if ever wanted.

## The bindings substrate (store + lints)

New `DraftMixin`-adjacent store ops (chunk-level, kind-agnostic):

- `bind_element(node_chunk_id, element_id, target_handle, relation='depicts')`
  — upsert a chunk→chunk link with `meta.element = element_id`.
- `unbind_element(node_chunk_id, element_id)` — retire the row.
- `element_bindings(node_chunk_id) -> [{element, kind, ident, chunk_id,
  relation, title}]` — `chunk_connections` generalized to carry `meta.element`
  and to return the target chunk handle (not just its ref).

Two lints added to the diagram core (both `LintFinding`s, beside compile and
SVG-bounds):

- **dangling-binding** — a binding whose `meta.element` has no matching id in
  the current source (drift after an edit removed/renamed the element).
- **coverage (soft)** — bound-element set vs. the diagram's meaningful element
  set; in the mermaid verify case, node set vs. linked-phase set. Advisory,
  surfaced in context, not a hard gate.

## Slices

1. **Chunk-binding substrate (dark, kind-agnostic). — BUILT.** Migration
   `0064` seeds `depicts` / `depicted-in` into `relations`. `bind_element` /
   `unbind_element` / `element_bindings` / `set_element_bindings` store ops on
   `DraftMixin` (`_draft_ops.py`) — one link row per (source, target,
   relation) edge, the depicting element id(s) in `links.meta.elements` (a
   set), so two elements on one target share a row. Ref-level and chunk-level
   targets both supported (`resolve_handle`). `set_element_bindings` reconciles
   a full desired set (the turn's `links` array) and skips unresolvable
   handles. Tests: `tests/test_element_bindings.py`. No UI; ships dark.
2. **Figure element bindings + prepared context. — BUILT.**
   `render_diagram_context` (`figure/context.py`) lists every element (id +
   tag + geometry) with its bound chunk + inlines the linked chunk bodies;
   `svg.elements()` + `svg.lint_bindings()` (dangling-binding lint, kind
   `'binding'`); the turn feeds the context into `build_prompt`, merges the
   binding lint (`_all_findings`), and reconciles the model's `links` array
   via `set_element_bindings`; `FigureHandler.link(element=,target=,mode=)`
   binds/unbinds and `get()` shows `## Bindings`; `/figure` renders a chip per
   bound element (server + turn-JSON refresh). Skills `precis-figure-svg`
   (the `links` field) + `precis-figure-help` updated. Tests:
   `tests/test_figure_bindings.py`. **This slice delivers the rich figure
   editing environment** and stands without mermaid.
3. **Factor the shared diagram core. — BUILT.** `src/precis/diagram/`:
   `lang.py` (the `DiagramLang` port + the shared `LintFinding`/`Element`
   value types), `turn.py` (the generic draw-with-me loop + `build_prompt` +
   `TurnResult`, all taking `lang`), `context.py` (the generic
   `render_diagram_context`). `figure/svg.py` gains `SvgLang`/`SVG_LANG` (the
   SVG instance — delegates to its own functions + carries the SVG prompt
   strings) and re-exports the value types; `figure/turn.py` +
   `figure/context.py` are thin shims binding `SVG_LANG` (so the handler,
   route, and every test are untouched). The parity guard is the full existing
   figure suite passing unchanged *through* the core; `tests/test_diagram_core.py`
   adds a throwaway non-SVG `_ToyLang` proving the core is language-generic (no
   SVG leaks). Pure refactor, no behavior change.
4. **Mermaid kind. — BUILT.** Migration `0066` (kind + `mermaid_node`/
   `mermaid_vocab`/`mermaid_notes`/`mermaid_turn` + `mermaid-of`/`has-mermaid`);
   handle codes `mm`/`mn`. `src/precis/mermaid/mermaid.py::MERMAID_LANG` — the
   second `DiagramLang`: validate/render/export via **pure-Python `mermaidx`**
   (embedded QuickJS + resvg, lazy-imported behind the `[mermaid]` extra), node
   extraction a source scan (works without the engine), `click` stripped, no
   bounds (topology as coords). `handlers/mermaid.py` (put/get/edit/delete/link
   + node binding); `mermaid/turn.py` shim; web `/mermaid`
   (`precis_web/routes/mermaid.py` + templates) rendering server-side SVG
   through figure's `sanitize_svg`, degrading to source text when the engine is
   absent. Dispatch registration env-gated `PRECIS_MERMAID_ENABLED` (dark).
   Skills `precis-mermaid-help` + `precis-mermaid`. Tests `tests/test_mermaid.py`
   (+ `tests/precis_web/test_mermaid.py`), mermaidx-backed compile guarded by
   `importorskip` so the gate stays green without the extra.
   **Handler factored (post-4):** the get/put/edit/delete/link CRUD lives once
   in `src/precis/diagram/handler.py::DiagramHandler` (generic over `DiagramLang`
   — the lang gained 6 handler-config attrs: `ref_prefix`/`node_prefix`/
   `project_relation`/`medium`/`render_value`/`element_noun`; bounds/viewBox axis
   gated on `default_bounds() is not None`). `FigureHandler`/`MermaidHandler` are
   now ~50-line subclasses (LANG + spec), so ~700 lines of near-dup CRUD
   collapsed to one base — figure/mermaid are two instances at *every* layer
   (turn loop, context, and now the handler), not copies. Parity guard: the
   figure + mermaid handler suites pass unchanged.
5. **Tick executor. — BUILT.** `diagram_propose` job_type
   (`workers/job_types/diagram_propose.py`) on the derived-job lane: params
   `{kind: figure|mermaid, ref_id, instruction, seeds:[handle]}`; the dispatcher
   resolves the diagram, composes a seed-augmented message (`compose_message`
   inlines seed chunk bodies), and runs **one turn** of the shared loop via the
   figure/mermaid shim — which *mutates the diagram in place* and reconciles the
   node→chunk bindings (unlike `cad_propose`, the turn loop is the apply
   mechanism), then writes a `job_result`/`job_summary`. Owned by the diagram
   artifact (compute lane) — `figure`/`mermaid` opt in via `KindSpec.can_own_jobs`.
   Registered in the job-type registry; the model call degrades to chat-only on
   failure. Tests `tests/test_diagram_propose.py`. Scenarios A (build-from-seeds)
   and B (verify) are now an autonomous, dispatchable tick.

Slice 2 is the high-value core and stands alone; mermaid (3–4) and the
autonomous tick (5) are separable follow-ons.

## Migrations / schema touchpoints

- `0064` — `INSERT INTO relations` for `depicts` / `depicted-in`
  (chunk-level). No new tables/columns — the `links.src_chunk_id` /
  `dst_chunk_id` / `meta` columns already exist.
- `0066` — register the `mermaid` kind + its chunk kinds + `mermaid-of` /
  `has-mermaid` relations, mirroring `0057`/`0058` (figure). Additive; reuses
  the `chunks` / `chunk_events` substrate.

## Skills to write

- `precis-mermaid` — the pinned prompt skill (prepended to every mermaid
  turn), the mermaid analogue of `precis-figure-svg`: the three docs you
  maintain, node naming for addressability, the supported diagram types,
  binding elements to chunks, and "coords are topology here, not geometry".
- `precis-mermaid-help` — the public kind reference (verb surface, the three
  model-owned docs, the turn log, lints, `mermaidx` render/export).
- Update `precis-figure-help` + `precis-figure-svg` for the new bindings
  affordance (elements can be bound to chunks; the prepared context).

## Deploy prerequisites

- `[mermaid]` extra installed on the web host(s) and the agent-profile node
  (melchior) — `mermaidx` + transitive wheels, no compiler.
- `PRECIS_MERMAID_ENABLED=1` where the kind should be live (mirrors the
  chem/route `PRECIS_CHEM_ENABLED` un-darking).

## Rejected alternatives

- **`mmdc`/mermaid-cli in a container on the derived-job lane.** The obvious
  "rent the heavy kernel at export" move (like cad/structure/pcb), and not
  wrong — but `mermaidx` makes it unnecessary: pure-Python, in-process, no
  Node/Chromium, runs on the Mac turn host where a container path is awkward,
  and covers validation *and* render *and* export. The container remains the
  fallback behind the `DiagramLang` port if `mermaidx` is ever unviable.
- **A hand-written Python mermaid grammar.** A faithful clone of mermaid's
  per-diagram Jison grammars would rot against upstream forever. We keep only
  a tiny source scan for node/edge *extraction* (needed for bindings), never
  as the validation authority — `mermaidx` (the real mermaid.js) is the
  authority.
- **Browser round-trip as the validation authority.** Viable (mermaid.js in
  the page reports parse errors) but it only covers the interactive web turn,
  not the MCP `put`/`edit` path or the autonomous tick, and it complicates the
  "never persist a broken source" invariant. `mermaidx` validates everywhere,
  in-process. Client-side mermaid.js stays available purely as a live-preview
  fidelity fallback.
- **Binding in the source** (`data-chunk=` / mermaid `click`). Single source
  of truth and survives export, but invisible to the knowledge graph and needs
  sanitizer changes. Rejected for the hybrid.
- **Binding in the link table only** (no reliance on the source `id=`).
  Cleanest graph story but element ids drift silently. The hybrid keeps the
  source `id=` as the join key and lints drift.
