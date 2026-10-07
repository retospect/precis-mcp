---
status: ready
title: memory recall / walk / keep — the three agent affordances over graph memory, one neighbourhood shape shared with the web
pillar: memory-graph
prio: high
model: opus
---

# memory recall / walk / keep — what an agent does with graph memory

## Motivation / why

Reto's R17 ruling (`memory-file-mirror.md` §Boundary): files and graph
coexist; the real harness import and the cutover stay held. The mirror
(`cli/memory_mirror.py::import_mirror` / `export_mirror`, on main since
aa669f59f, synthetic acceptance td470555) gives every topic file a
`SPACE:repo-dev` node keyed by `meta.file_mirror.{namespace,filename}`.
The native write path (anchored `edit`, `put` with `meta.hook`) passed its
live dogfood on R15 (td470292). What is missing is the *agent side*: once
the nodes exist, nothing tells an agent how to find one at the right
moment, how to see what sits around it, or which of three write paths to
use while files and graph coexist. td470555 names this as the next step:
"bounded recall/walk/keep contract spec, review before skill/source
implementation". The fleet wind-down (2026-10-07) moved it to local
agents.

The three gaps, each measured against code on main (readiness vet
2026-10-07 confirmed the anchors):

- **Recall.** `search(kind='memory', q=…, tags=['SPACE:repo-dev'])` works,
  but a hit renders title + body gist; it does not show the handle-form
  index line (`meta.hook`) or the mirrored file name, so an agent cannot
  match a graph hit to the topic file the harness may also have recalled.
  The session-start render (`cli/memory.py::render_memory_index`) is a
  plain DB list (no embedder) and is silent until a cutover that is now
  held, so no recall happens at session start at all.
- **Walk.** The renderer is built: `utils/eye_render.py::_render_note_eye`,
  `_rings`, `_first_hop` and `_recall` render a memory at every rung
  including `+recall`, and `tests/test_eye_render.py::test_memory_eye_1hop_shows_link_neighborhood_by_relation`
  covers it. Only the handler blocks it: `handlers/memory.py::MemoryHandler.get`
  accepts the base views (`links`/`log`/`raw`) plus `argument` and raises
  `Unsupported` for `fisheye`; `handlers/finding.py::FindingHandler.get`
  has the `extent_ladder` branch to copy. `fisheye-everywhere.md` AC 3
  (`+recall` on memory) is unmet for that reason alone.
  `web-graph-navigation.md` slice 1 (neighbourhood JSON) is `ready` and
  unbuilt; it wants one store-level neighbourhood function that panel,
  focus page and agent tooling share. No such function exists in `src/`;
  `_first_hop` issues per-relation `links_for` calls.
- **Keep.** Three write paths land `SPACE:repo-dev` nodes and nothing says
  which an agent uses when: `put(kind='memory', meta={'hook':…})`
  (native, no file), anchored `edit` on a mirrored node (graph-side edit
  of a file-born memory), and `memory mirror import` (file-side). The
  mirror export writes only nodes carrying `meta.file_mirror` (the
  namespace query in `cli/memory_mirror.py::export_mirror`), so a native
  node is never exported and is invisible to the files. Title/hook
  changes on a mirrored node are refused at export (no metadata policy).
  The 146 live `SPACE:repo-dev` nodes created by the one-shot importer on
  2026-10-03 (`cli/memory.py::import_memory_dir`, now stale against the
  files) carry no `file_mirror` key; the mirror "never adopts legacy
  imported nodes by title", so a real import would double every memory.

## In scope

Four slices, each shippable alone; 1a and 1b are independent of each
other, 2 and 3 are independent of everything above.

- **1a. The neighbourhood function (also `web-graph-navigation.md` slice 1).**
  `store/_links_ops.py::Store.neighbourhood(kind, ref_id, *, depth=1|2,
  rels=None, kinds=None, since=None, until=None, trust=None, cap=…)`
  returning `{focus, nodes:[{kind,id,label,state}], edges:[{src,dst,rel,dir}],
  counts:{rel:{kind:n}}}` from one query over `links ⋈ refs`, applying
  the same inverse rule `Store.links_for` applies (inverse rows are not
  all stored). Lives beside `links_for` because that is where the inverse
  rule is. The web route `GET /graph/<kind>/<id>.json` stays in
  `web-graph-navigation.md` as a thin wrapper. Rewiring
  `eye_render._first_hop` onto it is a **separate follow-up** (it changes
  the render for every link kind, `finding` included, and touches
  `tests/test_eye_render.py` / `tests/test_finding.py`), filed when 1a
  lands, not here.
- **1b. The memory walk.** `MemoryHandler.get` gains the extent ladder as
  `FindingHandler.get` has it: `view='fisheye'|'fisheye+1hop'|'fisheye+2hop'`,
  any rung `+recall`, dispatching to `eye_render.render_eye`. Ring
  headings follow `refeye.RING_GROUPS` as built: a `SPACE:repo-dev`
  node's `part-of` section renders under **Parts** and its `related-to`
  siblings under **Notes & links**; no new group. `_render_note_eye`
  renders `meta.file_mirror.filename` beside the handle when present
  (`me4641 (worker_busy_vs_starved_diagnosis.md)`).
  `+recall` on memory respects the `SPACE:` axis: `_recall` passes the
  focus node's `SPACE:` value through to `search_chunks_semantic` as a
  tag filter (add a `tags=` kwarg there, matching how `kinds=` narrows),
  so k is filled from the right scope rather than post-filtered down.
  `precis-fisheye-help`: `memory` moves from "not wired" to live;
  `applies-to` and `kinds:` gain `memory`. `fisheye-everywhere.md` AC 3's
  `extent='+recall'` is read as `view='fisheye+1hop+recall'` (the MCP
  spelling); that item's AC text is corrected in the same commit.
- **2. Recall — the index line is the hit.**
  - `MemoryHandler` gains a `search` override declaring `view: str | None`
    explicitly (today `NumericRefHandler.search` has no `view` kwarg and
    `**_kw` would drop it silently; `runtime/dispatch.py` intercepts only
    `dreamable`/`stubs`/`chase-queue`). `view='index'` renders each hit as
    the session-start bullet `- <Title> (me<id>[, <filename>]) — <hook>`
    where `<filename>` is `meta.file_mirror.filename` when present and
    `<hook>` is `meta.hook`, else the first body line. Any other `view`
    value on memory search is `BadInput` listing `index` (raised in the
    override); non-memory kinds keep today's behaviour. Default view is
    unchanged.
  - `cli/memory.py::render_memory_index` gains `--q <text> --k N`: the
    bullet render restricted to the N best hits of
    `MemoryHandler.search(q=…, tags=['SPACE:repo-dev'], page_size=N)`
    (hybrid search through the handler, which owns the embedder the CLI
    lacks). Without `--q`, output is byte-identical to today (the session
    hook and `--export-dir` cache are unaffected; the filename suffix is
    added only in the `--q` and `view='index'` renders).
  - `precis-memory-help`: one "Recall what I already know" section:
    `search(kind='memory', tags=['SPACE:repo-dev'], q='<task>',
    view='index')`, then `get(view='fisheye+1hop+recall')` on the hit.
- **3. Keep — the coexistence write rule and two mirror reports.**
  - **Rule (decision below, and in `precis-memory-help`):** while the
    harness memory dir is the file of record, a memory that has a file is
    edited *in the graph* with the anchored `edit` once the real mirror
    import has run for its namespace, and in the file before that; the
    mirror export is how the file catches up. A memory with no file is
    created with `put(kind='memory', tags=['SPACE:repo-dev','section:<slug>'],
    meta={'hook':…})` and stays graph-only until the export policy
    (open question 1) exists. No graph-first flip is part of this item.
  - `export_mirror` returns a `MirrorReport` (today an `int`; the CLI's
    `exported N files` line is kept from `report.created`) with a new
    `unexported: list[str]` of handles: live `SPACE:repo-dev` nodes with
    no `file_mirror` key, or with another namespace. `MirrorReport` gains
    the field with an empty default; `tests/test_memory_mirror_acceptance.py`'s
    pinned JSON key set is updated in the same commit. Export still writes
    nothing for them.
  - `import_mirror(…, legacy='refuse'|'retire'|'keep')` (default
    `refuse`) and the matching `--legacy` CLI arg in
    `cli/memory.py::add_parser`: `refuse` lists live `SPACE:repo-dev`
    nodes without `file_mirror` whose title equals a topic file's `name:`
    and raises `ImportRefused`; `retire` soft-deletes them in the import
    transaction; `keep` imports beside them. The flag is built and tested
    on the synthetic fixture only; running it on prod is Reto's go
    (td471883).
  - `precis-memory-help` drops "safe to re-run" as the way to seed
    repo-dev nodes; the mirror is the import path, the one-shot importer
    is the cutover tool.

## Explicitly NOT in scope

- The real harness import, the cutover, and retiring any file class
  (`memory-file-mirror.md` §Boundary; `memory-native-authoring.md` holds
  the cutover sequence).
- A metadata policy that exports native nodes to new files, and any
  graph-first write rule (open question 1; the mirror item defers it to
  "a later explicit reconciliation design"). This item only *reports*
  native nodes.
- Rewiring `eye_render._first_hop` onto `Store.neighbourhood` (follow-up
  after 1a; touches every link kind).
- The web panel, focus page SVG, path finding and trail
  (`web-graph-navigation.md` slices 2–6) and that item's route for slice 1.
- `extent=` on kinds other than `memory` (`fisheye-everywhere.md` in-scope
  2 owns the totality).
- The `focus` verb and render→act loop (`fisheye-level2.md`).
- Dreaming, session transcripts, the resident/discovered split
  (`context-memory-hierarchy.md`), and any change to the harness's own
  file-relevance recall.
- Namespaces for `SPACE:research` memory; the mirror is repo-dev only.

## Acceptance criteria

1. (1b) `get(kind='memory', id=M, view='fisheye+1hop')` on a node with a
   `part-of` section and `related-to` siblings renders them under
   **Parts** and **Notes & links** with filenames; `view='fisheye+2hop'`
   renders second-hop counts; `+recall` appends k ≤ 8 nearest, none
   carrying the other `SPACE:` value (fixture: one repo-dev and one
   research node with near-identical bodies). A new test in
   `tests/test_memory.py` pins the render (today's `Unsupported` is pinned
   by no test); `search_chunks_semantic(tags=…)` has its own unit test.
2. (1a) `Store.neighbourhood` returns the slice-1 shape; a test asserts
   that for one fixture node the node and edge sets equal the union of
   `links_for(direction='out')` and `links_for(direction='in')` after the
   inverse rule, and that `depth=2` on a hub with >200 second-hop edges
   returns counts under the cap, not rows.
3. (2) `search(kind='memory', tags=['SPACE:repo-dev'], q=…, view='index')`
   renders hits as index bullets with handle, filename and hook;
   `view='nope'` is `BadInput` naming `index`; `precis memory index --q …
   --k 5` prints the same five lines; without `--q` the output equals
   today's render byte for byte on the fixture.
4. (3) On the synthetic mirror fixture (`tests/test_memory_mirror_acceptance.py`,
   120 topics + index): `export_mirror` after one native `put` reports
   that handle under `unexported`; `import_mirror` with a planted legacy
   node refuses by default, `legacy='retire'` retires it and imports,
   `legacy='keep'` imports beside it; byte-for-byte export of the 121
   files is unchanged by any of the three.
5. (2) **Recall number**, gate-runnable: `tests/test_memory_recall_fixture.py`
   checks in 10 task queries with one expected topic each over the
   synthetic fixture and asserts `--q --k 5` hits ≥ 8/10. The number is
   also logged in this item's decisions log by the builder (manual, not
   the gate). This is the native-vs-mirror comparison
   `memory-native-authoring.md` AC 5 wanted.
6. `precis-fisheye-help` lists `memory` as live and `precis-memory-help`
   has the recall section and the coexistence write rule
   (`tests/test_skill_ingest.py::test_shipped_skill_corpus_has_zero_gate_findings`
   stays green);
   `tests/test_deploy_tree_no_secrets.py` stays green.

## Target + blast radius

- 1a: `src/precis/store/_links_ops.py` (`Store.neighbourhood`), tests
  `tests/test_link_crud.py` (owns the `links_for` tests)
- 1b: `src/precis/handlers/memory.py::MemoryHandler.get`,
  `src/precis/utils/eye_render.py::_render_note_eye`, `::_recall`,
  `src/precis/store/_chunks_ops.py::search_chunks_semantic` (`tags=`),
  `src/precis/data/skills/precis-fisheye-help.md`,
  `docs/backlog/fisheye-everywhere.md` (AC 3 spelling), tests
  `tests/test_memory.py`, `tests/test_eye_render.py`
- 2: `src/precis/handlers/memory.py` (new `search` override),
  `src/precis/cli/memory.py::render_memory_index`, `::add_parser`,
  `src/precis/data/skills/precis-memory-help.md`, tests
  `tests/test_memory.py`, `tests/test_memory_import.py`, new
  `tests/test_memory_recall_fixture.py`
- 3: `src/precis/cli/memory_mirror.py::export_mirror`, `::import_mirror`,
  `::MirrorReport`, `src/precis/cli/memory.py::add_parser`/`run`
  (`--legacy`), `src/precis/data/skills/precis-memory-help.md`, tests
  `tests/test_memory_mirror.py`, `tests/test_memory_mirror_acceptance.py`
- `docs/backlog/web-graph-navigation.md` (slice 1 pointer here, done),
  `docs/backlog/threads/INDEX.md` (seam, done)

## Open questions / decisions log

- **[open, non-blocking for this item — Reto, td471883]** Metadata policy
  for exporting a native node to a new file: proposed `name:` =
  slugified title, `description:` = `meta.hook`, `metadata.type` =
  `project` unless the author sets a new writable meta key `type` ∈
  {user, feedback, project, reference}. Until decided, native nodes are
  reported, not exported, and new memories with a harness home are still
  written as files (the coexistence rule). No slice here waits on it.
- **[open, non-blocking]** Whether `view='index'` should also exist on
  `get(kind='memory', id='/recent')`; start without.
- **[decision 3, recommendation — needs Reto's go, destructive on prod; td471883]**
  Before the real mirror import, retire the 146 legacy `SPACE:repo-dev`
  nodes from the 2026-10-03 one-shot import (`--legacy retire`). They are
  stale against the files since the cutover was reverted (found
  2026-10-07; `memory-native-authoring.md` decisions log) and the mirror
  would otherwise double every memory. Soft delete, recoverable; the
  alternative (`keep`) leaves two hits per topic in every recall. The
  flag itself ships on fixtures without the go.
- **[decided 2026-10-07, this spec]** The walk's data source is one
  store-level neighbourhood function shared with the web (the
  orchestrator's "same neighbourhood shape as web slice 1", 2026-10-06);
  `web-graph-navigation.md` slice 1 is built here and its route consumes
  it. Rationale: one inverse rule, one cap, one test; the web panel and
  the MCP ring must never disagree about what is linked. The rewire of
  the existing text render onto it is a follow-up, so 1b can ship on the
  renderer that already exists.
- **[decided 2026-10-07, this spec]** `+recall` respects the `SPACE:`
  axis by filtering inside the semantic search (`tags=`), not after it,
  so k stays full. A repo-dev agent asking "what else is about this" must
  not get research notes, and a research session must not get fleet
  gotchas.
- **[decided 2026-10-07, this spec]** Recall is a render of search, not a
  new verb; walk is a view on `get`, not a new verb; keep is a rule plus
  two mirror-report additions. No new kind, verb, table or migration.
- **[readiness vet 2026-10-07 → folded]** Verdict was needs-work (4
  blockers, 7 advisories, split suggested); every finding is resolved in
  the text above: the mirror meta key is `filename` not `name`; AC 1
  names `tests/test_memory.py` (there is no memory row in
  `test_kind_totality.py`); `view='index'` gets an explicit `search`
  override on `MemoryHandler` because `NumericRefHandler.search` has no
  `view` kwarg; the graph-first branch left slice 3 so open question 1 is
  non-blocking; the walk gap is restated as the handler allowlist only;
  the `_first_hop` rewire became a follow-up; ring headings are Parts /
  Notes & links as built; the SPACE filter is in-search; `--q` goes
  through the handler; `export_mirror`/`import_mirror` signature changes
  and the acceptance test's key set are named; AC 5 checks in its
  queries; AC 6 lost the non-deterministic top-hit clause; the
  `extent=`/`view=` spelling is reconciled. Split taken as 1a/1b/2/3.
- **Dependencies, stated honestly.** On `memory-file-mirror.md`: shipped
  on main (R17), not yet deployed; slices 1a/1b/2 do not need it, slice 3
  does. On `file-mirror.md` in-scope 5 (recall fixture): not needed, AC 5
  checks in its own 10 queries. On `web-graph-navigation.md`: slice 1
  moves here; nothing else.
