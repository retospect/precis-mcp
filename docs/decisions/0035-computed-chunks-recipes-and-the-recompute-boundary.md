# 0035 — Computed chunks: payload + recipe, sandboxed execution, and the recompute boundary

* Status: **Accepted** (design plan; build sequenced in §6, not yet implemented)
* Date: 2026-06-22 (resolved 2026-06-22)
* Refines: [0034 — figure assets, data supplements, permission provenance](0034-figure-assets-and-permission-provenance.md) (fills in its deferred *render step*, §3, and generalizes `figure_data`/`figure_code`)
* Continues: [0033 — draft chunks as an editable document](0033-draft-chunks-editable-document.md)
* Related: [0007 — derived queue, no blocking jobs](0007-derived-queue-no-block-jobs.md) · [0017 — derived queue family](0017-derived-queue-family.md)

## Context

ADR 0034 made figures chunk-native and **deferred the render step** (§3) and
the data→plot pipeline. Working through it (Reto, 2026-06-22) surfaced a more
general shape than "a graph plots a table", driven by three observations:

1. **The render needs real code, not a chart spec.** A declarative chart
   grammar (Vega-Lite) covers statistical plots but not the scientific general
   case — **rendering an atomic / crystal-structure model**, a molecular
   diagram, a custom domain figure. So **the render code (Python) lives in the
   graph chunk** (Reto's decision); a declarative spec is at best a fast-path
   for simple charts, not the mechanism.
2. **Data has provenance and a regeneration story.** Data is often **ingested
   from process output** (a simulation, a DFT relaxation, an experiment,
   another script), so the **data chunk should record how to regenerate it** —
   not just hold the numbers.
3. **A graph can plot multiple data chunks**, so it re-renders when **any**
   source changes — a multi-input dependency that must not metastasize into a
   reactive build system / programming language.

This generalizes 0034's per-figure `figure_data` (private payload) and
`figure_code` (deferred) into one concept and answers "where does the code
go / does this become a programming language?".

## Decision

### 0 — The unifying shape: a *computed chunk* = payload + recipe + inputs

Both data and graphs are the **same kind of thing**: a cached **payload**, a
**recipe** that (re)produces it, **input links** to what it derives from, and a
**content-addressed invalidation key**.

| | payload (cached) | recipe | inputs |
|---|---|---|---|
| **data chunk** (`chunk_kind='table'`/data) | `meta.table` JSON (or `chunk_blobs` if large) | `meta.regen` — generating process / command / sim params / ingest source | inert `derived-from` → upstream data (if any) |
| **graph chunk** (`chunk_kind='figure'`) | rendered image in `chunk_blobs` | **render code (Python) in `meta.render`** (or a declarative spec for the chart fast-path, §2) | `plots` → ≥1 data chunks |

This is "a notebook cell that emits an artifact", but **content-addressed and
sandboxed** (§3) and with a **bounded dependency semantics** (§4).

### 1 — Canonical data lives in one chunk, and records its own regen

A `chunk_kind='table'` (data) chunk is the **single source of truth** for a
dataset:

* **`meta.table = {header, rows}`** is canonical (cells addressable `[1,4]` or
  `['population','Beijing']`); large/binary data spills to `chunk_blobs`
  (0034 §1).
* **`chunks.text` is a derived projection** — the markdown render of the data,
  regenerated on write, never hand-edited (the existing `summary`/`keywords`/
  `ord<0`-card pattern). One source, no drift; small tabular data stays text so
  it embeds and is `numerics`-indexed.
* **`meta.regen`** records where the data came from and how to rebuild it: the
  generating process / command / parameters / ingest pointer, plus inert
  `derived-from` links to any upstream data. This is **provenance +
  reproducibility metadata**, not a live trigger (§4).

Generalizes 0034's `figure_data` into a **shareable, canonical** data chunk
that multiple views reference.

### 2 — The render recipe lives in `meta.render`; `text` is the caption

A `figure` chunk carries **`plots` links to ≥1 data chunks** and a render recipe
that is one of two forms:

* **Declarative spec (the fast-path)** — `meta.render = {kind:'scatter'|'line'|
  'bar', x:<col>, y:<col>, …}`. The renderer is **our trusted code over the
  data chunk's untrusted numbers**, so it runs **in the system worker with no
  sandbox** (§3). Covers the common statistical chart; ships *before* the
  sandbox exists.
* **Code (the general case)** — `meta.render = {kind:'code', lang:'python',
  src:'…'}`. Python (matplotlib, plotly, ASE/py3Dmol for structures, RDKit for
  molecules, …) — the general mechanism precisely because the target isn't
  always a chart (atom models, custom viz). This is **untrusted chunk-authored
  code** and runs only in the sandbox (§3).

Invariants:

* **`chunks.text` is always the human-readable, embeddable projection** — for a
  figure that is the **caption / legend** (so figures are findable by caption).
  Code never lives in `text`; it is structured data in `meta.render`. One figure
  = one chunk (text=caption, `meta.render`+`plots`) plus an `ord<0` blob child
  for the rendered pixels.
* The recipe is **content** (the reproducible spec / code) and a **job input**
  (§3) — never run by the live document path. Because it lives in `meta`,
  recipe edits are **logged to `chunk_events`** to preserve recipe history (meta
  does not diff as cleanly as chunk text).
* The trusted/untrusted split — *spec = our code over their data (in-worker);
  code = their code (sandboxed)* — is what lets the declarative fast-path ship
  ahead of the Docker work (§6).

### 3 — Execution is a *sandboxed job*; the system worker only schedules + caches

Running arbitrary code (a render, or a data regen) **must not** happen inline in
an `edit()` (ADR 0007 discipline) and **must not** run in the trusted
per-minute system worker (that would be RCE on every node). So:

* The rendered image is an **`ord<0` derived chunk** (blob in `chunk_blobs`) —
  `ord<0` marks it regenerable / out-of-reading-order / safe to DELETE+INSERT.
* **Invalidation key:** `hash(recipe_sha, sorted(input_content_shas))`. For a
  graph that's `hash(render_code_sha, sorted(plotted_data_shas))` — the
  multi-input case: any plotted table's `content_sha` change → key mismatch →
  stale.
* **Two render paths, one cache.** A declarative-spec figure (§2) renders with
  **our trusted renderer in the system worker** — no sandbox, the input is data
  not code. A code figure mints a **sandboxed render `kind='job'`** on the
  existing executor layer.
* **The sandbox is Docker** (resolves old open #1). A dedicated **render image**
  carries the scientific stack (matplotlib, plotly, ASE/py3Dmol, RDKit); the job
  is `docker run` with `--network none`, read-only rootfs, tmpfs `/tmp`,
  non-root, `--memory`/`--cpus`/`--pids-limit`, dropped caps, seccomp default —
  code in, image bytes out, no filesystem escape, no ambient network (any
  agent-supplied fetch would still go through `safe_fetch`, but the default is
  no network at all). The trusted worker never executes chunk-authored code; it
  schedules and caches. **Routing constraint:** render jobs run only where
  Docker exists — the cluster Macs run workers via pip/launchd without Docker,
  so the sandboxed-render daemon lives where the agent profile already does (cf.
  melchior), not on every node.
* **Invalidation is lazy + access-gated** (the "render when stale *and* gotten"
  decision). Marking is cheap and eager: a data-chunk edit walks
  `links_for(data_chunk, relation='plots', direction='in')` and clears each
  figure's cached key (pure SQL, no rendering). **Rendering is deferred to
  access** via the derived-queue discipline (ADR 0007 — readers never block):
  a stale figure sits in a claim queue (`cached_key != computed_key`) just like
  `embed`/`chunk_keywords`; a `get()` returns the *last-good* image with a
  "regenerating" marker and the render job catches up. We do **not** eagerly
  re-render the unviewed corpus; the claim queue is gated on an access signal
  (figure in an open draft / read in last N days), with **export as the
  strongest access context** (§4a).
* Data regeneration is the same shape but **never automatic** (§4): re-running a
  data chunk's `meta.regen` is an **explicit sandboxed job** that writes a new
  data-chunk version.

### 4 — The recompute boundary (the load-bearing invariant)

> **`plots` is the only *live, reactive* recompute edge. It points figure →
> data, one hop, acyclic. Every other edge — `derived-from`, `regen`
> provenance, `cites` — is inert: re-run only by an *explicit job*, never
> auto-triggered.**

* Editing a plotted data chunk auto-marks its figures stale (a render job
  follows). That is the *only* automatic recomputation.
* A data chunk does **not** auto-regenerate when its upstream process inputs
  change — regen is a deliberate job. So data→data `derived-from` chains are
  **history**, not a reactive dataflow.
* **Acyclic by construction:** data never depends on figures; a figure can't
  plot a figure. No evaluation order, no fixpoint, no cycle detection.

This is the line that keeps it from "becoming a programming language": the
**document's dependency model stays declarative and one-hop**, even though
**artifact *generation* runs arbitrary code in a sandbox**. The
Turing-completeness is confined to sandboxed jobs that emit content-addressed
artifacts and **cannot mutate document structure or create live cycles**. A
build DAG with one reactive edge (`plots`) and explicit-only regen is not a
spreadsheet.

### 4a — Export is the render barrier (lazy snaps back to eager, document-scoped)

Lazy rendering is right for browsing, but **export must ship a PDF where every
figure is current** — no stale images, no "regenerating" placeholders. Export is
therefore the strongest access context, and it forces freshness through the
existing child-job fence rather than any new mechanism:

1. **Staleness sweep.** The `draft_export` job walks the draft's chunks; for each
   `figure` it recomputes `hash(render_code_sha, sorted(plotted_data_shas))` and
   compares to the cached key. Content-addressed → figures already fresh from
   browsing are skipped; when nothing drifted the sweep mints nothing and export
   compiles immediately.
2. **Mint a render job per stale figure, as a child of the export todo** (spec
   figures render cheap in-worker, code figures via the Docker job — same render
   the lazy queue would mint, forced now).
3. **Block on the children.** `meta.auto_check = {'type':'child_job_succeeded'}`
   — export stays not-doable until all renders succeed, then compiles. A
   pipeline with a barrier: render-all → barrier → compile.
4. **A failed render fails the export precisely** via the `child-failed:<job_id>`
   bubble pointing at *the figure that wouldn't render* and its error — not a PDF
   with a silently stale/missing image.

Two invariants keep it honest:

* **Pin to a snapshot.** Capture `(figure → computed_key)` at the barrier and
  render *to those input shas*; a concurrent data edit during export doesn't tear
  the render (figure A on old data, B on new) — it just marks figures stale for
  the *next* export. Renders are reproducible because content-addressed by input
  sha.
* **Export forces `plots`, never `regen`.** The barrier re-renders figures from
  *whatever the current data is*; it does **not** re-run any data chunk's
  `meta.regen` (no silent recompute of a DFT relaxation / regression behind the
  author). Stale-vs-upstream data is the author's explicit job. Even under the
  export hammer the only edge touched is the one reactive edge — §4 holds.

### 5 — Transforms are jobs that emit data chunks

Any *computed* dataset — `total=sum(A)`, filter, join, a regression fit, a
simulation — is a **job** that reads input data chunks and writes a **new data
chunk** with an inert `derived-from` link to its inputs and a `meta.regen`
recording the computation. Figures then `plots` that computed chunk. Code that
*transforms data* and code that *renders an image* are both "recipes executed
by sandboxed jobs"; neither lives on the live document path.

### 6 — Build sequence

The trusted/untrusted split (§2) re-sequences the work so most of it ships
*before* the Docker sandbox — only chunk-authored code is gated on it:

1. **Data/table chunk** — `meta.table` canonical + derived markdown `text` +
   `meta.regen`. No execution, no sandbox. Unblocks supplement tables
   immediately.
2. **Declarative chart fast-path** — `meta.render={kind:'scatter'|'line'|'bar'}`,
   trusted in-worker renderer, `plots` links, the lazy mark-stale / claim-queue /
   last-good-on-`get()` cache. Graphs without the sandbox.
3. **Export render barrier** — fold the §4a staleness sweep + child-render fence
   into the existing `draft_export` job.
4. **Docker render sandbox + code figures** — the render image and `docker run`
   harness, then `meta.render={kind:'code'}`. Gated on nothing chunk-authored
   running until this lands.
5. **Transforms / regen jobs** (§5) and the declarative↔code split for data
   regen — last.

## Consequences

* One concept (computed chunk = payload + recipe + inputs) subsumes 0034's
  `figure_data` and `figure_code` and extends to data provenance/regen and to
  non-chart rendering (atom models, molecular/structural viz).
* **New cost — a code sandbox.** Choosing code-in-chunk over a declarative
  grammar means a real sandboxed execution environment (resource/time caps, no
  ambient network/filesystem) for render + regen jobs. This is the principal
  new engineering surface and security boundary; it is the price of generality
  and is mandatory before any chunk-authored code is executed.
* The document's live graph is provably shallow (one hop) and acyclic →
  invalidation is plain cache-key checking, no scheduler.
* Reproducibility is first-class: every artifact (data or image) carries the
  recipe and inputs to rebuild it; clearance/audit (0034 §4) and "regenerable
  from data" fall out.
* Embedding cost stays bounded: data text + render code + small data embed;
  image/large binaries never do.
* **Addressing (ADR 0036).** Every computed chunk — a `figure`, a data
  `table`, a `figure_code`/`figure_data` recipe chunk — is a draft chunk, so
  its handle is the ADR 0036 computed `dc<chunk_id>` (a pure function of
  `chunk_id`; the `derived-from` / `plots` edges are links, not part of the
  handle). The `ord<0` rendered-image variant is a derived chunk and stays
  unaddressed (regenerable, out of reading order). **Current state:** draft
  chunks (figures included) now address by `dc<chunk_id>` end-to-end (ADR
  0036's draft slice has landed); the recompute/sandbox machinery here is
  unaffected by the addressing scheme.

## Resolved (2026-06-22)

1. **Sandbox technology → Docker.** A dedicated render image + `docker run`
   harness (`--network none`, read-only rootfs, resource/pid caps, dropped caps);
   render jobs run only where Docker exists (the agent-profile host, not the
   pip/launchd Macs). Still the gate: nothing chunk-authored runs until it lands
   (§3, build step 4).
2. **Declarative fast-path → yes, and it ships first.** `scatter`/`line`/`bar`
   specs render with our trusted code over the data chunk's numbers, so they run
   in-worker with no sandbox — graphs ship ahead of the Docker work (§2, §6).
   Code figures remain the general mechanism for non-charts.
3. **Render code location → inline `meta.render`; figure `text` = caption.** No
   separate `figure_code` chunk; recipe edits logged to `chunk_events` to keep
   history (§2).
4. **Invalidation timing → lazy + access-gated, export-forced.** Mark stale
   eagerly+cheap, render via the claim queue, serve last-good on `get()`; export
   is the barrier that forces every referenced figure fresh (§4a).

## Still open

* **Reactive data regen stays out of scope.** Auto-regenerating downstream data
  when an upstream process input changes (true reactive dataflow) is deliberately
  *not* built; it would add a second reactive edge and reintroduce build-system
  semantics. Revisit only via a dedicated ADR.
* **Access-gating signal** for the lazy render queue (open-draft membership vs
  last-read recency vs both) — pick when step 2 is built; export (§4a) does not
  depend on it.
```
