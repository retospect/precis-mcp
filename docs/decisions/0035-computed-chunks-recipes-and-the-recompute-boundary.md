# 0035 — Computed chunks: payload + recipe, sandboxed execution, and the recompute boundary

* Status: **Draft / proposed** (design plan; not yet implemented)
* Date: 2026-06-22
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
| **graph chunk** (`chunk_kind='figure'`) | rendered image in `chunk_blobs` | **render code (Python) in the chunk** | `plots` → ≥1 data chunks |

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

### 2 — The render recipe (code) lives in the graph chunk

A `figure` chunk carries its **render code** (Python — matplotlib, plotly,
ASE/py3Dmol for structures, RDKit for molecules, …) and **`plots` links to ≥1
data chunks**. The code is the general mechanism precisely because the target
isn't always a chart (atom models, custom viz).

* The code is **content** (diffable via `chunk_events`, searchable, the
  reproducible recipe) and a **job input** (§3) — it is *not* run by the live
  document path.
* A **declarative spec is an optional fast-path** for the common statistical
  chart (cheap, no sandbox, trivially cacheable). It does not replace code; it's
  an optimization for the cases that don't need code.

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
* A **system pass marks figures stale** (reverse `plots` walk:
  `links_for(data_chunk, relation='plots', direction='in')`) and **mints a
  sandboxed render `kind='job'`** (the existing executor layer that already runs
  `claude -p` jobs). The job runs the chunk's code in a sandbox (resource +
  time limits, no ambient network — agent-supplied fetches go through
  `safe_fetch`, no filesystem escape) and writes back the image artifact. The
  trusted worker never executes chunk-authored code; it schedules and caches.
* Data regeneration is the same shape: re-running a data chunk's `meta.regen` is
  an **explicit sandboxed job** that writes a new data-chunk version.

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

### 5 — Transforms are jobs that emit data chunks

Any *computed* dataset — `total=sum(A)`, filter, join, a regression fit, a
simulation — is a **job** that reads input data chunks and writes a **new data
chunk** with an inert `derived-from` link to its inputs and a `meta.regen`
recording the computation. Figures then `plots` that computed chunk. Code that
*transforms data* and code that *renders an image* are both "recipes executed
by sandboxed jobs"; neither lives on the live document path.

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

## Open decisions

1. **Sandbox technology** for render/regen jobs (container, nsjail, a
   restricted interpreter, the existing job executor hardened). The gating
   requirement, not a detail — nothing chunk-authored runs until this exists.
2. **Declarative fast-path?** Offer a Vega-Lite-style spec for simple charts
   (no sandbox, cheap) alongside the code path, or keep one code mechanism for
   uniformity. Lean: add the fast-path later if simple charts dominate.
3. **Reactive data regen is explicitly out of scope.** Auto-regenerating
   downstream data when an upstream process input changes (true reactive
   dataflow) is deliberately *not* built; it would add a second reactive edge
   and reintroduce build-system semantics. Revisit only via a dedicated ADR.
4. **Where the render code lives physically** — inline `meta.render` on the
   figure chunk vs a linked `figure_code` chunk (0034). A separate chunk diffs
   and embeds cleanly; inline is simpler. Lean: separate chunk, `derived-from`.
```
