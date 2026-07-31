---
status: draft
title: sim-harness — precis drives external Pareto-sim repos as quests (verify → ingest → write up)
model: opus
---

# sim-harness — precis drives external simulation repos as quests

> **Scope discipline.** The full design is a **consumer layer** on machinery
> precis already has — `quest` (striving + append-only logbook + the `serves`
> DAG), the derived-job lane (ADR 0044), `sandbox_run` (container substrate,
> built dark), and the `paper`/`figure`/`draft`/`citation`/`material` corpus.
> **Slice 1 (this proposal's buildable subset) is a plain CLI tool** — no
> worker, job, or dispatch — that proves the contract and the verify loop. The
> quest-driven *automation* (a `level:recurring` watch wrapping the verbs) is
> **net-new, deferred to slice 2**. The container-run + in-container-lit-search
> half is **already designed** as `sandbox_run` `mode:run` + `precis_access:read`
> — this proposal **builds on it, does not reinvent it**, and defers to it (see
> NOT in scope).

## Motivation / why

Reto keeps a growing family of standalone Python **Pareto trade-study
simulators** — `flyinghose` (aerial firefighting hose+drone), `flowsim` (milled
flow-plate), `lighterthanair` (no-gas LTA) — and more will follow. They share an
opportunity even where their file shapes differ:

- each carries **engineering data with provenance debt**. `lighterthanair`'s
  `materials.yaml` is the sharpest case: 33 entries, every one flagged
  `verified: false`, and its own header states values *"MUST be re-verified per
  entry against a datasheet / precis-mcp"* — **the integration is already
  specified in the sim, waiting for the other half here.** `flyinghose/data/*.yaml`
  are plain catalogs today (no `verified:` scheme) — a natural adopter once the
  scheme exists; `flowsim`'s parameters live in Python (`flowsim/config.py`), out
  of scope for YAML verify. So slice 1 concretely targets **`lighterthanair` +
  a fixture**, and the manifest lets other sims opt in by declaring `verify:`
  files later — the "why" does not depend on a uniformity the family doesn't yet
  have;
- each authors a **findings artifact** (`docs/findings.md`, `out/SUMMARY.md`,
  headline-findings sections) and emits **derived outputs** (Pareto CSVs,
  PNG/VTI/VTU plots).

Today these are islands. Nothing keeps their data honest against the literature,
nothing pulls related work in, nothing turns a run into a written, cited summary
— so a finished sim quietly **rots into an abandoned side project**. The durable
fix is not a one-shot script; it is to make each sim a **quest** precis owns: a
standing aim whose recurring watches re-verify data and re-write the summary as
literature and the sim itself evolve. This proposal lays the contract and ships
the first fully-buildable loop (**ingest + verify**, as CLI verbs), seeding one
quest as the anchor; the recurring automation, the write-up, and the container
run land on top in later slices.

## The shape (the whole design; slices below carve the buildable subset)

```
 git repo  (SOURCE OF TRUTH: code + editable YAML DB + regenerated artifacts)
     │  precis.sim.yaml  { run · outputs · verify · writeup }
     ▼
[precise per-sim image] ──run──▶ outputs ──ingest──▶ precis corpus
     ▲   (sandbox_run mode:run,          (markdown/plaintext, searchable;
     │    blocked slice)                  provenance SHA in meta; plots deferred)
     │                                          │
     │  writeback: verified:true, source:[pa..] │  read-only lit search
     └──── verify verb ◀────────────────────────┘
                     │  appends a deed
                     ▼
             precis QUEST  (striving + logbook)
                     │   slice-2 automation = a level:recurring watch
                     ▼   wrapping the verbs   (net-new; NOT quest_tick)
             ingest · verify · writeup  re-run as inputs drift
```

**Three integration jobs, not one** — kept distinct on purpose:

1. **ingest** — blobify the sim's prose findings + tabular results into the
   searchable corpus (the `markdown`/`plaintext` kinds, both chunked+embedded),
   recording the producing git SHA in each ref's `meta` for provenance. Binary
   plots are deferred (see NOT in scope).
2. **verify** — for each low-confidence YAML entry, lit-search precis
   (read-only) → judge value + citation → **write back** to the repo YAML
   (`verified: true`, `source: [pa…]`), mint a `material` (canonical) + a
   `citation`, and append a `deed` to the sim's quest logbook. *Testable,
   already scoped in `lighterthanair`.*
3. **writeup** *(slice 2)* — compose a `draft` from findings + verified data +
   cited related work; refresh when the SHA or the verified-set drifts.

### Data locality — split by role, nothing important lives only in the DB

| Thing | Source of truth | Representation in precis |
|---|---|---|
| Sim code + editable YAML DBs | **git repo** (on disk) | read by verify; **written back** by verify |
| Derived artifacts — findings + CSV | git repo (regenerable) | **blobified + searchable** — projected into `PRECIS_ROOT` then chunk+embedded as `markdown`/`plaintext` via the prose-ingest walker (`_ensure_ingested`, not the create-only `put()`); producing git SHA in `meta`; idempotent by the walker's content gate |
| Derived artifacts — binary plots (PNG/VTI/VTU) | git repo (regenerable) | **deferred** — no binary-blob `put` today; `folder`-kind home lands with the sandbox harvest slice |
| Verified property values | dual-written: repo YAML **and** `material` + `citation` | canonical + cited |
| Generated summary (slice 2) | `draft` kind (composed), may graduate to `paper` | the write-up |

The repo stays fully reconstructable without precis; precis holds a **searchable,
citable, verified mirror** plus the literature context. We do **not** blobify the
repo wholesale — only the derived snapshots.

### Container granularity — one *precise* image per sim, for the drive path (slice 3)

The eventual **drive path** invokes **one pinned, reproducible image per sim**
(freeze its `requirements.txt`/`pyproject`). The three sims' deps genuinely
conflict and are heavy (`flowsim` wants PyVista+OpenGL+LBM; `lighterthanair`
wants pymoo+scipy; `flyinghose` is bare numpy), and a run must reproduce for a
given SHA — a shared kitchen-sink image makes runs non-reproducible. A separate
**permissive "unlimited-pips" image** is for *authoring* a sim, never the drive
path. precis is its own container already. **This is exactly `sandbox_run`'s
`image` param + harvest contract** — the sim image is a `code-task`-style image
tag; we add nothing to the container mechanics. Slice 1 does not run any sim; it
reads a local checkout.

## In scope (slice 1 — a CLI tool, buildable now, no container, no worker)

`precis sim …` is a new CLI verb group invoked **directly** (by a human, or later
by slice-2 automation). It reads a locally-available checkout named in the
registry. It adds **no** job_type, executor, dispatch path, or worker pass —
"workers untouched" in Target+blast-radius is therefore accurate and consistent.

1. **`precis.sim.yaml` manifest + validator** — a schema (`run`, `outputs`,
   `verify`, `writeup` keys) with a loader that fails closed on missing/ill-typed
   keys. `lighterthanair` gets the first manifest committed **to its own repo**.
2. **Sim registry** — a precis-side config (a data file) mapping
   `slug → {path, git_remote, manifest, quest}`, where `quest` is the quest id
   (or slug) this sim's verify runs report to. This is the seam by which precis
   learns the set of sims **and** the sim↔quest linkage AC #6 needs. Topology:
   reach-out, separate repos (decided below).
3. **`precis sim ingest <slug>`** — **project** the manifest's prose/CSV
   `outputs:` into `PRECIS_ROOT` under a `sim/<slug>/` prefix (findings
   `.md/.markdown` as a `markdown` file, CSV as a `plaintext` file), then run
   the **existing prose-ingest walker** — `handler._ensure_ingested(slug,
   force=…)`, the same path `precis jobs ingest` uses (`cli/ingest.py:
   _ingest_one_kind`) — to chunk+embed them into the searchable corpus. This
   deliberately **does not use the public `put()` verb**: `PlaintextHandler.put`
   is create-only and raises on a second call; `_ensure_ingested` is the
   mtime/sha256 content gate that makes re-ingest idempotent (unchanged file →
   no-op; changed file → re-chunk). Record the producing git SHA in each ref's
   `meta`. *(Extension note: `plaintext` stores under `.txt`/`.log`/`.bib`, not
   `.csv`, so a CSV lands as `<slug>.txt` — content preserved and searchable.)*
   **Binary plots (`.png/.vti/.vtu`) are skipped this slice** — no binary-blob
   ingest exists (`figure` is SVG-text only; `datasheet` is PDF-only,
   `supports_put=False`); plots are referenced by path in the findings until the
   `folder`-harvest slice.
4. **`precis sim verify <slug>`** — for each `verify:` YAML entry with
   `verified: false` or `confidence` below a floor: run a **read-only** precis
   lit-search, an LLM judge returns `{value_ok, citation_ref, note}`, then (a)
   write back `verified: true` + `source:` to the YAML and **git-commit** the
   delta on a `precis-verify/<date>` branch, (b) mint a `material` + a `citation`
   in precis, and (c) append a `deed` to the registry-named `quest`'s logbook
   (`quest_log`). A `--dry-run` produces the writeback records + the exact YAML
   diff with **no network, no git, no precis writes**, for the gate.
5. **Seed one `quest`** (striving) for `lighterthanair` — *"keep the no-gas LTA
   material library verified and its findings written up"* — and record its id in
   the registry entry (item 2). That registry field **is** the sim↔quest link;
   verify's `deed` (item 4c) is what makes the quest reflect real progress.
6. **Docs**: `docs/design/sim-harness.md` (manifest schema + registry + the full
   three-job design as the durable reference), a CLAUDE.md subsystem map entry,
   `state-map.md`, and a `precis-sim-help` skill stub.

## Explicitly NOT in scope

- **Container-run / recurring / in-container lit-search.** Running the sim image
  and letting the container call precis read-only is **`sandbox_run` `mode:run` +
  `precis_access:read`** (design: `docs/design/sandbox-run.md`, §"Re-run &
  operationalize"; harvest is slice 2 there). This proposal's container path is
  **blocked-by** those slices and is deliberately excluded here; slice 1 reads a
  local checkout via the CLI instead.
- **The quest-driven automation.** Wrapping ingest/verify/writeup in a
  `level:recurring` **watch** (per `glossary.md`) under the quest is **net-new,
  slice 2** — and is *not* the existing `quest_tick` coordinator loop
  (`src/precis/quest/loop.py`), which a builder must not reach for by mistake.
  Slice 1 proves the verbs and seeds the quest but schedules nothing.
- **Binary plot ingest** (PNG/VTI/VTU as first-class searchable/citable refs) —
  no binary-blob `put` exists today; the natural home is a `folder` kind, which
  is exactly what the `sandbox_run` harvest slice produces. Deferred to that
  slice; slice 1 references plots by path in the findings.
- **`writeup` draft generation** — slice 2 of *this* harness; depends on `ingest`
  landing first so there is a corpus to cite.
- **`flowsim` Python-config verify and `flyinghose` provenance-scheme adoption** —
  the manifest allows them to opt in later by declaring `verify:` files; slice 1
  targets `lighterthanair` + a fixture only.
- **Migrating the sims into precis** (submodules/monorepo) — rejected, decisions
  log.

## Acceptance criteria

Green in the container gate (`ruff` + `mypy` + `pytest`), network/git/precis-write
steps behind `--dry-run` fixtures:

1. The manifest loader parses a valid `precis.sim.yaml` and raises a clear error
   for each missing/ill-typed required key (`run`, `outputs`, `verify`,
   `writeup`). Unit test.
2. `precis sim list` reads the registry and prints registered sims with resolved
   paths **and** their linked quest id; an unreachable path is reported, not
   crashed. Unit test.
3. `precis sim ingest <fixture-sim>` projects the fixture's prose+CSV `outputs:`
   into `PRECIS_ROOT` and runs the prose-ingest walker (`_ensure_ingested`, **not
   `put()`**), chunk+embedding them as `markdown`/`plaintext` refs (**no
   `figure`/`datasheet`**, binary plots skipped) with the producing git SHA in
   `meta`; a second run over unchanged files is a **no-op** (zero new rows) via
   the walker's content gate. Integration test.
4. `precis sim verify <fixture-sim> --dry-run` produces, for each flagged entry,
   a record `{entry, value_ok, citation_ref, note}` and the exact YAML diff it
   *would* commit, touching no network, no git, no precis writes. Integration
   test.
5. `precis sim verify lighterthanair` (live, manual acceptance, not gated):
   ≥ 5 `materials.yaml` entries flip to `verified: true` with a real `source:`
   citation resolvable in the corpus, committed on a `precis-verify/<date>`
   branch; a `material` and a `citation` exist in precis for each.
6. After the item-5 run, the registry-linked `lighterthanair` `quest` is `active`
   and its `quest_log` contains a `deed` for the verify run (written by item 4c).
   The link is the registry `quest` field — no `link()` against a non-ref is
   attempted.
7. `docs/design/sim-harness.md`, the CLAUDE.md subsystem map, `state-map.md`, and
   a `precis-sim-help` skill stub are added/updated in the same commit.

## Target + blast radius

- **New:** `precis sim` CLI group (`src/precis/cli/sim.py`), manifest
  loader/validator, registry loader + data file, ingest/verify verbs,
  `docs/design/sim-harness.md`, `precis-sim-help` skill, tests + a fixture sim.
  In the sims: `lighterthanair/precis.sim.yaml` (committed to that repo).
- **Edited:** CLI registration, CLAUDE.md subsystem map, `state-map.md`.
- **Reuses (not modified):** the prose-ingest walker
  (`cli/ingest.py:_ingest_one_kind` → `handler._ensure_ingested`) and the
  `markdown`/`plaintext` handlers behind it, the `material` + `citation`
  handlers, the `quest` handler + logbook (`append_entry`). `precis sim ingest`
  calls the walker path, not the create-only `put()`.
- **Not touched:** search core, embeddings, web, **all workers** (slice 1 has no
  job/dispatch), `sandbox_run` (only referenced). Adds a **CLI surface + a seeded
  quest**; changes nothing in existing prod flows until a verb is run.
- **Build prerequisite:** `material` landed on `main` (`3fd81492`, 2026-07-29);
  this worktree is behind. The build merges `main` first (as `/land` does) so
  the `material` mint is unconditional, not feature-detected.

## Open questions / decisions log

- **Repo topology — DECIDED: separate repos, harness reaches out.** The registry
  holds `{slug, path, git_remote, quest}`; sims stay independent repos with their
  own release cadence and `.claude` dev loops. *Rejected: submodules / monorepo*
  — couples release cadence, bloats the precis tree with heavy sim deps and
  output blobs, buys nothing slice 1 needs (verify reads a local checkout; ingest
  is content-addressed either way). Revisit only if reproducible co-versioning
  becomes a hard requirement for a live `mode:run`.
- **Slice-1 execution — DECIDED: a plain CLI tool.** `precis sim ingest/verify`
  are CLI verbs invoked directly, no job_type/executor/dispatch/worker. Resolves
  the earlier contradiction between the prose ("agent job on the laptop worker")
  and the CLI-shaped ACs — the ACs were right; the prose was wrong. The
  melchior-only `agent` worker profile is *not* involved in slice 1. Dispatched
  execution arrives only with slice-2 automation, on top of these verbs.
- **`material` minting — DECIDED: unconditional.** `material` is live on `main`;
  verify mints it alongside `citation` + YAML writeback. The prior "iff that kind
  is live" hedge is void (it was also unbuildable — nothing specified how to
  feature-detect at runtime).
- **Ingest kinds + mechanism — DECIDED (v2/v3 correction):** findings→`markdown`,
  CSV→`plaintext` — both chunked+embedded, so ingested findings are
  searchable/citable. **Mechanism (v3):** project the files into `PRECIS_ROOT`
  and drive the **prose-ingest walker** (`_ensure_ingested`, as `precis jobs
  ingest` does), **not** the create-only public `put()` — that is what makes
  re-ingest idempotent (mtime/sha256 gate) and is the load-bearing correction to
  the earlier "supports_put=True … idempotent" wording, which named two
  mutually-exclusive paths. Binary plots (PNG/VTI/VTU) are **skipped this
  slice**: no binary-blob ingest exists (`figure` is SVG-text-only per ADR 0057;
  `datasheet` is PDF-only, `supports_put=False`); their home is a `folder` kind,
  deferred to the sandbox-harvest slice. Rejected the v1-rewrite's
  `paper`-via-`MarkupInput` (only jats/elsevier/arxiv-html/latex) and
  `plots→figure`. *(CSV stores under `.txt` — `plaintext`'s extension set omits
  `.csv`; content preserved and searchable.)*
- **Sim↔quest link — DECIDED:** the registry `quest` field, not a precis `link()`
  (a sim/registry entry is not a ref). Verify writes a `deed` to that quest's
  logbook; that is the only quest mutation slice 1 makes.
- **Manifest home — DECIDED:** `precis.sim.yaml` lives **in each sim repo**. It is
  the sim's declaration of how it wants to be driven; the registry only points at
  it. Keeps the sim self-describing and portable.
- **Bundle ingest + verify, or split? — DECIDED: bundle, build ingest first.**
  They share the manifest+registry infra and are one coherent "wire this sim in"
  deliverable. Build order within the slice: manifest+registry → ingest (no LLM,
  no external commit) → verify (LLM judge + external git commit + quest deed), so
  the riskier half rests on a proven base. Split into 1a/1b only if the gate on
  verify's external-commit path proves it needs its own review.
- **`writeup` altitude (slice 2):** graduate the draft to a `paper`, or keep a
  mutable `draft`? Lean: `draft` until a human promotes it — a sim summary is
  living, not archival. *(Resolve when slice 2 is specced.)*
- **Verify judge trust — DECIDED (slice 1):** auto-commit flips to a
  `precis-verify/<date>` branch (never the sim's default branch) — review is the
  merge. Human sign-off before the merge, not before the commit.

### `ready` pass history

- **v1 (needs-work → resolved):** first `ready` pass raised 4 blockers
  (execution-model contradiction; `datasheet` not `put`-able; stale `material`
  optionality; unbuildable quest-link/deed AC) + 2 advisories (motivation
  overstated family uniformity; "campaign" misused for non-existent quest
  machinery). All are addressed in this revision — see the DECIDED entries above.
  Verified-clean by that pass and retained: `sandbox_run` built-but-dark +
  `mode:run`/`precis_access:read` genuinely unbuilt (blocked-by is honest);
  `paper`/`figure`/`draft`/`citation` real and `put`-able; quest
  striving/logbook/serves-DAG/deed vocabulary accurate. A re-run of `ready` is
  warranted before flipping `status: ready`.
- **v2 (needs-work): all 4 v1 blockers hold on re-check, but ingest's kind
  mapping introduces 2 new blockers.**
  - **blocker — `findings (.md) → paper via MarkupInput` is not buildable as
    written.** `MarkupInput.fmt` (`src/precis/ingest/add.py:139-171`) is
    constrained to `MARKUP_FORMATS = {"jats", "elsevier_xml", "arxiv_html",
    "latex"}` (`src/precis/ingest/markup.py:49-51`); `parse_markup` raises
    `MarkupParseError: unknown markup format` for anything else. A plain sim
    findings doc (confirmed real: `flyinghose/docs/findings.md`,
    `flowsim/out/*/SUMMARY.md`, `lighterthanair/STATUS.md` — all plain
    Markdown) matches none of the four formats. The kind that actually
    ingests a `.md` file is the separate `markdown` kind
    (`src/precis/handlers/markdown.py`, a `PlaintextHandler` subclass) via
    the file-kind CLI walk (`src/precis/cli/ingest.py` `_PROSE_KINDS`), not
    `paper` via `precis_add`/`MarkupInput`. AC #3 and the "Ingest kinds —
    DECIDED" entry need correcting to either target `markdown` (not `paper`)
    or add a markdown branch to `MarkupInput`/`parse_markup` (itself
    out-of-slice, `paper`-ingest-subsystem work the Motivation doesn't
    establish a need to touch).
  - **blocker — `plots (.png/.vti/.vtu) → figure` is not buildable as
    written.** `figure` (`src/precis/handlers/figure.py`) is the ADR-0057
    **interactive SVG-canvas** kind; `DiagramHandler.put`
    (`src/precis/diagram/handler.py:240`) accepts only `text=<svg source>` /
    `vocab=` / `viewbox=`, `corpus_role='none'` — there is no binary/blob
    ingest path for a raster PNG or a VTK `.vti`/`.vtu` file. Checked every
    `supports_put=True` handler in `src/precis/handlers/`: none accepts an
    arbitrary binary blob (`folder` is a pure organizational container with
    no file body). This also contradicts Target+blast-radius's "Reuses (not
    modified)" claim for `figure` — making it accept plot files would need a
    handler change, which is declared out of scope. AC #3 and the "Ingest
    kinds — DECIDED" entry need a real target kind for plots (or a new one)
    before this is buildable.
  - **advisory — CSV→`plaintext` "content-addressed to git SHA" phrasing
    overreaches what the handler does.** `plaintext`
    (`src/precis/handlers/plaintext.py`) is file-rooted under `PRECIS_ROOT`
    with mtime/sha256-of-file re-ingest gating (`_ensure_ingested`), not the
    git-SHA-keyed content-addressing `paper`/`figure` ingest would use. The
    idempotency AC (#3) is probably still satisfiable through that gate, but
    the spec should say so explicitly rather than imply one uniform
    addressing scheme across all three kinds.
  - **Re-verified clean (v1 fixes hold):** execution model is now
    consistent — no lingering job/worker/dispatch language in the slice-1
    sections (Scope-discipline box, In-scope item list, AC, Target+blast
    radius all agree: CLI verbs only, "workers untouched" is accurate).
    `material` confirmed live on `main` (`3fd81492`, also present at current
    `main` tip `75fd8f44`) with a real `handlers/material.py`,
    `supports_put=True` — unconditional mint is buildable once the worktree
    merges `main`. Quest deed: `precis.quest.logbook.append_entry`
    (`src/precis/quest/logbook.py`) is a real, callable append path;
    `entry_type='milestone'` *is* the "deed" per its own docstring; quests
    default `STATUS:active` on creation
    (`handlers/quest.py: default_tags_on_create`) — AC #6 is buildable as
    written. Advisories: Motivation's per-sim claims verified against the
    actual repos — `lighterthanair/materials.yaml` has exactly 33
    `verified: false` entries and the header quote ("MUST be re-verified per
    entry against a datasheet / precis-mcp") matches verbatim;
    `flyinghose/data/*.yaml` genuinely has no `verified:` scheme;
    `flowsim/flowsim/config.py` genuinely holds the params in Python — no
    overstated uniformity remains. "campaign" only appears now inside this
    history section quoting the old finding, not in the live spec. No new
    contradiction found against sibling proposals (`sandbox-run-substrate.md`
    status:built matches this proposal's characterization of `sandbox_run`
    as built-dark with `mode:run`/harvest still unbuilt) or against
    `docs/decisions/0044-derived-job-lane.md` / `docs/architecture/glossary.md`
    (`watch` definition matches). `precis sim` CLI namespace confirmed
    genuinely new (no existing `src/precis/cli/sim*`). The one still-open
    decisions-log entry ("`writeup` altitude") is explicitly slice-2/deferred
    and doesn't bear on slice-1 buildability — not blocker-severity for this
    proposal.
- **v2 blockers resolved:** ingest kind mapping retargeted to the real,
  `put`-able searchable kinds — findings→`markdown`, CSV→`plaintext` (both
  `MarkdownHandler(PlaintextHandler)`, chunked+embedded) — and binary plots
  moved to NOT-in-scope (no binary-blob `put` exists; `folder` home lands with
  the sandbox-harvest slice). The `paper`-via-`MarkupInput` and `plots→figure`
  paths are struck from In-scope item 3 / AC #3 / the "Ingest kinds — DECIDED"
  entry. The CSV advisory is addressed by stating idempotency comes from the
  file-rooted handler's own content gate and the git SHA lives in `meta`. A
  confirming `ready` re-run is warranted before `status: ready`.
- **v3 (needs-work): both v2 kind-mapping blockers verified fixed and hold;
  the idempotency-mechanism wording the CSV fix introduced is itself
  inaccurate and creates 1 new blocker (plus 1 advisory).**
  - **blocker — "supports_put=True … idempotent via the handler's own content
    gate" names two mutually exclusive code paths as one.** Confirmed
    `MarkdownHandler`/`PlaintextHandler.put()` (the `supports_put=True` verb
    the item text explicitly invokes) is create-only and raises
    `BadInput("file already exists: …")` unconditionally on a second call to
    the same slug, with **no content comparison**
    (`src/precis/handlers/plaintext.py:692-712`, `_put_create`). The actual
    mtime/sha256 content gate ("re-ingest of an unchanged file inserts
    nothing") lives in the private `_ensure_ingested()` method
    (`plaintext.py:1366-1405`), which `put()` never calls to decide
    whether to write — it's only exercised lazily on `get`/`search`, and by
    the *existing* `precis jobs ingest` walker
    (`src/precis/cli/ingest.py:_ingest_one_kind`), which explicitly
    bypasses `put()` and calls `handler._ensure_ingested(slug,
    force=force)` directly against a file it expects already on disk. AC #3
    ("a second run inserts zero new rows") is only achievable if `precis sim
    ingest` mirrors that second, private-API pattern (write bytes to the
    resolved workspace path, then call `_ensure_ingested` directly) — but
    the item's own phrasing points a builder at the public `put()` verb,
    which fails AC #3's second-run claim with an uncaught `BadInput`
    instead of a graceful no-op. In-scope item 3 / AC #3 / the data-locality
    table need to say explicitly which write path `precis sim ingest` uses
    and, if it's the `_ensure_ingested`-direct pattern, that it does not go
    through `put()`.
  - **advisory — CSV → `plaintext` silently loses its `.csv` extension on
    disk.** `PlaintextHandler._EXTENSIONS = (".txt", ".log", ".bib")`
    (`plaintext.py:255`) — `.csv` is not recognized by `_resolve_path`'s
    `preferred_ext` sniff (`put`, lines 671-680) or by `_walk_files`, so a
    CSV written via this kind lands on disk as `<slug>.txt` (content
    preserved, extension normalized). Not a buildability blocker — the
    ingested content is still searchable/citable, matching the kind's
    actual contract — but the spec should say the CSV is stored as
    plaintext-with-normalized-extension, not implicitly "as a `.csv`".
  - **Re-verified clean:** the kind retarget itself is accurate —
    `MarkdownHandler(PlaintextHandler)` confirmed
    (`src/precis/handlers/markdown.py:46`), both `supports_put=True`
    confirmed in each `spec`, `_PROSE_KINDS = ("md", "plaintext", "tex")`
    confirmed real (`cli/ingest.py:145`) establishing `markdown`/`plaintext`
    as legitimate, already-wired prose kinds. Binary-plots-deferred is
    consistent everywhere it appears (scope-discipline box, shape diagram,
    data-locality table, In-scope item 3, NOT-in-scope, AC #3, the
    Ingest-kinds decision entry) — `figure` reconfirmed SVG-text-only
    (`handlers/figure.py` / `diagram/handler.py:240`, `put` takes
    `text=<svg>`/`vocab=`/`viewbox=` only) and `datasheet`
    reconfirmed `supports_put=False` (`handlers/datasheet.py:87`); no
    lingering plot-ingest claim anywhere in the current text. All 4 v1
    blockers still verified resolved: no lingering job/worker/dispatch
    language outside the history section (grep confirms "workers untouched"
    is the only live claim); `material` still absent from this worktree but
    present at `main` tip (`e6a74c61`) with `supports_put=True`
    (`src/precis/handlers/material.py:71` on `main`), matching the stated
    "merge main first" prerequisite; `quest.logbook.append_entry` still a
    real callable path (`src/precis/quest/logbook.py:60`); no "campaign"
    language outside the history section. Reuses/Not-touched lists stay
    consistent — the new idempotency finding doesn't require modifying
    `markdown.py`/`plaintext.py` (the fix belongs in the new `precis sim`
    CLI code choosing which existing method to call), so "Reuses (not
    modified)" is not contradicted. No new external contradiction found
    against sibling `docs/proposals/*.md` (none reference
    `PlaintextHandler`/`MarkdownHandler`/`_ensure_ingested`).
- **v3 blocker resolved:** In-scope item 3, AC #3, the data-locality table, the
  "Ingest kinds — DECIDED" entry, and the Reuses list now state the mechanism
  explicitly — `precis sim ingest` **projects the files into `PRECIS_ROOT` and
  drives the prose-ingest walker (`_ensure_ingested`, as `precis jobs ingest`
  does), not the create-only `put()`** — which is the path that actually
  delivers AC #3's idempotent no-op re-ingest. The `.csv`→`.txt` extension
  normalization is now stated (advisory addressed). Everything the v3 pass
  re-verified clean (kind retarget, binary-plots-deferred consistency, all v1
  fixes, `material`-on-`main` prerequisite, quest deed) stands unchanged. A
  final confirming `ready` re-run is warranted before flipping `status: ready`.
- **v4 (clean — confirmed against code, targeted re-check only):** the v3
  fix holds and introduced nothing new.
  - **Re-verified:** all five locations (In-scope item 3, AC #3, the
    data-locality table, the "Ingest kinds — DECIDED" entry, Target+blast-
    radius Reuses) now consistently state `precis sim ingest` projects files
    into `PRECIS_ROOT` and drives `handler._ensure_ingested`, not `put()`.
    Confirmed against code: (a) `PlaintextHandler._ensure_ingested`
    (`src/precis/handlers/plaintext.py:1366`) is the real mtime/sha256
    content gate — a re-run with matching `mtime_ns` or matching `sha256`
    returns the existing `ref` early with no new insert; (b)
    `cli/ingest.py:_ingest_one_kind` (`src/precis/cli/ingest.py:218`) calls
    `handler._ensure_ingested(slug, force=force)` directly, never `put()`;
    (c) this genuinely satisfies AC #3's "second run inserts zero new rows"
    — both early-return branches in `_ensure_ingested` skip the insert path.
    Also confirmed `PlaintextHandler.put()` → `_put_create`
    (`plaintext.py:684-698`) does raise `BadInput` unconditionally on an
    existing slug with no content comparison, matching the spec's framing
    of why `put()` was wrong for AC #3. No lingering "supports_put=True …
    idempotent" or "via put()" claim survives as a *live* statement — every
    grep hit for that phrasing is confined to the `ready`-pass-history
    section quoting past findings, not the active spec text.
  - **CSV→`.txt` normalization:** stated in both In-scope item 3 (line 140)
    and the decisions-log entry (line 269), consistently phrased as
    "content preserved and searchable," addressing the v3 advisory.
  - **No regressions:** grep for `job_type`/`dispatch`/`worker`,
    `campaign`, `MarkupInput`, and `plots→figure` shows every hit for the
    old/rejected phrasings confined to the scope-discipline box, live
    NOT-in-scope/decisions-log entries that correctly reference them as
    rejected, or this history section — no lingering contradiction in live
    scope text. `material` unconditional mint (item 4, AC #5), quest-deed
    AC #6, and motivation accuracy all still read as v1/v2 left them.
    Reuses/Not-touched remain internally consistent — the fix lives in new
    `precis sim` CLI code choosing which existing method to call, not a
    handler modification.
  - **Verdict: clean, no blockers, no new advisories.** The only remaining
    step is a human flipping `status: ready`.
