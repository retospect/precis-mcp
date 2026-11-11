---
id: precis-spec-help
family: work
title: precis — specifications live in graph memory: the spec-in-memory format
summary: a specification is a hub of small linked memory nodes under the Specifications hub — hierarchy, spec root and section shapes, feature/ruling/gotcha/thread placement, slices as plan todos, link budget and lint, the transfer writer procedure, the prune pass against code, how each consumer reads a spec, and the supersede step on retirement
answers:
  - how is a specification stored in the graph, and where does it live?
  - what does a spec root node look like, and what goes in its body?
  - what does a spec section node look like, and how big may it be?
  - where do feature, ruling, gotcha and thread nodes live around a spec?
  - how do slices and plan todos carry a spec's execution state?
  - what are the link budget, the lint checks and the size rules for a spec?
  - how do I write or transfer a spec into the graph?
  - how do I prune a transferred spec against the code?
  - how do I read a spec, and how do the slicer, ready agent and builder each consume it?
  - how do I retire a file spec or a built section?
applies-to: get/put/edit/delete/link/tag (kind='memory'), search(kind='memory', args={'under': …}), get/search (kind='todo'), get (kind='python')
tags: [workflow]
kinds: [memory, todo]
status: active
---

# precis-spec-help — the spec-in-memory format

A specification is a **hub of small linked memory nodes** under the
Specifications hub `me568215` (itself part-of the repo-dev root
`me474312`). This implements Reto's ruling of 2026-11-05 (`me568168`):
specifications live in graph memory, not the filesystem, repo-wide, as
DRY densely linked structures. The format supersedes the earlier factory
limitation that kept specifications in repository files.

Two ground rules run through everything below:

- **One claim, one canonical owner.** Every other node links to or
  mentions the owner; nothing restates it. Current work status (what is
  being built, what landed) belongs on todos and gripes, never in memory
  prose.
- **Requirements carry their maturity.** Distinguish a settled contract,
  a proposed design, an unresolved decision and a verified implementation
  fact. An implementation fact needs a readable code or test anchor with
  provenance appropriate to the claim. A proposal is not made build-ready
  by giving it a title or linking a paper. A source-derived design states
  the application inference and cites the source separately.

Read one section with `get(kind='skill', id='precis-spec-help~<n>')`;
list them with `get(kind='skill', id='precis-spec-help/toc')`. The
transfer writer procedure is `precis-spec-help~8`; the prune pass is
`precis-spec-help~9`; consumers are `precis-spec-help~11`.

## How is a spec organised in the graph?

The Specifications hub `me568215` holds **programme roots** as its
children. Each programme contains its **spec roots**, a feature index,
cross-spec rulings, gotchas and campaign threads. A spec root contains
its sections directly or through design sub-hubs.

- Each node has exactly **one `part-of` parent**. Cross-topic relevance
  is a `related-to` edge or a handle mention: the Chemistry hub
  `me477660` links to Kinetics rather than taking a second parent. A
  thread hangs under its programme, not directly under the hub.
- Move a misplaced node by replacing its parent edge, preserving
  evidence and its other links.
- Enforcement is advisory, not a write gate: a `put`/`link`/`tag` that
  leaves a spec root (`spec:<slug>` + `section:index`) with no `part-of`
  chain to `me568215` acks `AUDIT:spec-root-parent` and stamps the
  system tag; a clean re-check clears it, and `scripts/memory-lint`
  check 15 lists every miss offline (gr631564).
- Programme membership plus the ordered root/section outlines are the
  navigable inventory. **No file index** is required or wanted.
- A legacy reference with no graph replacement becomes a clearly scoped
  unresolved dependency on the owning plan — never an invented complete
  spec standing in for unseen content.

## What does a spec root node look like?

A spec root is `section:index`, part-of its programme root, tagged
`spec:<slug>` and `SPACE:repo-dev`. Title form: `Spec: <slug>: <purpose>`.

```python
put(kind="memory", text="Spec: routing: task-aware MCP tool routing",
    link="memory:<programme root id>", rel="part-of",
    tags=["section:index", "spec:routing", "SPACE:repo-dev"],
    meta={"hook": "routing spec root"})
```

Body order: purpose; owner thread and quest handles; the plan todo
`td<N>` that carries state ("state lives on plan todo td<N>"); the
ordered section or sub-hub handles; scope exclusions; graph dependencies;
source gripes and evidence. A hub's `contains` ring is **unordered**, so
the body supplies the reading order. Large roots list sub-hubs, each of
which owns its own ordered outline (see the size rules,
`precis-spec-help~7`).

- Make the contract maturity explicit: proposed requirement, unresolved
  decision or verified behavior.
- A build-ready slice needs a concrete trigger, expected result and
  acceptance evidence; missing fixtures or thresholds are **named
  decisions on its plan**, not hidden assumptions.
- All required context must resolve through the graph. A missing legacy
  dependency gets a recovery item on the plan; it is not replaced by a
  confident filename-free paraphrase of unseen content.

## What does a section node look like, and how big may it be?

A section node is `section:reference`, part-of its spec root. The title
is the section's **claim in at most 12 words**, with no slug prefix — the
`spec:<slug>` tag and the hook carry "<slug> section <n>", and the spec
root body gives the reading order:

```python
put(kind="memory", text="Routing prefers cache hits over model calls",
    link="memory:<spec root id>", rel="part-of",
    tags=["section:reference", "spec:routing", "SPACE:repo-dev"],
    meta={"hook": "routing section 3"})
```

- **One design claim per node**, prose only, under about **3500
  characters** — the fisheye cuts a body at 4000 characters and `get`
  at 24 kB (a 116 kB file spec becomes roughly 30 nodes).
- Evidence is `[pa<N>]` mentions with **at most three typed `cites`
  edges**; rulings and gotchas are `[me<N>]` mentions. Fenced YAML or
  mermaid never goes in a body — that content lives in a plan or mermaid
  ref that the section links.
- A number next to a cite must appear in the cited text, or the
  ungrounded-number audit fires.

## Where do feature, ruling, gotcha and thread nodes live?

**Feature node** — `section:reference`, part-of the programme's
feature-list node. Title `Feature: <name>`. Body: what and why in one
paragraph, then its spec section, task, gripe, ruling and gotcha handles,
and the surface it renders on. One hub per programme, one node per
cross-spec feature: a feature **spans specs**, and slices serve a feature
*and* a section.

**Single-spec variant.** A section node may stand in for the feature and
slice todos serve it directly. Mint a feature node when a feature spans
specs or surfaces, and re-point the `serves` edges then (first used by
the task-aware routing spec `me568172`, 2026-11-05).

**Ruling node** — tagged `ruling` with meta `ruled_by`, `ruled_on` and
`review_by` ([[precis-memory-help]] §rulings), one decision per node,
linked `related-to` what it decides with edge meta
`rel_intent='decides'`. The meta lands only through `put(meta=…)` /
`edit(args={'meta': …})` on a `ruling`-tagged node — `meta=` on `tag`
is refused (a todo-only door there). A ruling left in prose is what
rots:

```python
put(kind="memory", text="<the decision, one claim>",
    link="memory:<spec root id>", rel="part-of",
    tags=["ruling", "spec:routing", "SPACE:repo-dev"],
    meta={"ruled_by": "reto", "ruled_on": "2026-11-05",
          "review_by": "2026-12-05"})
link(kind="memory", id=<ruling id>, rel="related-to",
     target="memory:<spec root id>", meta={"rel_intent": "decides"})
```

**Gotcha node** — `section:gotchas`, `qualifies` the feature or section
it bounds.

**Thread node** — `section:threads`, first line `Left: …` with every
handle on that same line; the 14-day sweep retires an untouched thread
node that has no carry-over todo.

**Placement (first conversion wave, 2026-11-05).** A ruling or gotcha
that concerns one spec is part-of that spec root; only **cross-spec**
rulings sit in a rulings sub-hub (`section:index`) under the programme
root, so no hub passes 40 members. A **transferred ruling** keeps meta
`ruled_on` as the original date; its status is `absorbed` where shipped
code already carries the decision (it drops out of recall but stays
readable), else `in-force` with **one `review_by` for the whole transfer
wave** (kinetics: 2026-12-05) passed explicitly — the default is floored
at today + 14 days (gr631565), which lands the wave on the transfer
date, not its chosen review date. Ruling and
gotcha titles carry no "Reto <date>:" or "Gotcha:" prefix; the tags and
meta say what they are.

## How do slices and plan todos carry a spec's execution?

Slices and tasks are **never memory nodes**: memory rejects the STATUS
axis and the fleet dispatches todos. One **plan root todo per spec**,
linked `plan-of` the spec root (functional — one plan per spec); slice
todos are its children; sequence is `blocked-by` edges; every parented
`put` carries `meta={'llm_tier': None}`, or a billed `plan_tick`
auto-dispatches ([[precis-todo-tree-help]]). Tags `thread:<slug>` and
`spec:<slug>`:

```python
put(kind="todo", text="Routing spec — plan", parent_id=<programme todo root>,
    tags=["thread:routing", "spec:routing"], meta={"llm_tier": None})
link(kind="todo", id=<plan root id>, rel="plan-of", target="memory:<spec root id>")
put(kind="todo", text="Slice: routing cache layer", parent_id=<plan root id>,
    tags=["thread:routing", "spec:routing"], meta={"llm_tier": None})
link(kind="todo", id=<slice A id>, rel="blocked-by", target="todo:<slice B id>")
```

- A slice todo **serves its feature node**, so the feature's fisheye
  shows `served-by`. The section owns the normative acceptance contract;
  the todo points to it and records slice-specific verification.
- If a builder cannot read the graph, its self-contained brief is a
  **snapshot naming the source handles and revision**, refreshed when
  those requirements change. A copied brief never becomes an independent
  specification.
- Status, landed sha, lease and lane live on the todo (`STATUS:`,
  `landed:` comment, `claimed-by:` lease, lane comment).
- Next doable slice for a programme:
  `search(kind='todo', view='doable', args={'under': <programme todo root>})`.

## What are the link budget, lint checks and size rules?

- Each node has **one `part-of` parent plus up to three deliberate
  evidence or relationship edges** (`cites`, `derived-from`, `qualifies`,
  `decides`…); additional cross-references are handle mentions in the
  body. Generated mention edges aid navigation but establish no normative
  precedence. A ruling's decision, a section's requirement and a task's
  status each have one canonical owner.
- Keep bodies **below the 4000-character fisheye limit**; section
  guidance is about 3500 characters. When a root approaches the limit,
  group sections into meaningful sub-hubs: the root lists the groups in
  reading order, each group lists its own sections in reading order, and
  you do not duplicate every descendant on the root as well.
- **More than 40 direct members requires grouping** (the sub-hub size
  rule); a smaller hub is still split when its body is unreadable.
- **Lint a spec node for**: unresolved handles, ungrounded numbers (a
  number next to a cite must appear in the cited text), several
  independent claims in one section, stale current-state prose, and
  snapshot duplication. Cite verified canonical code symbols
  (`python:<alias>::<qualname>`), never old commit shas or bare source
  paths. An unavailable dependency is an explicit unresolved item on the
  plan, never a silent omission.
- Navigation: the Specifications hub lists programmes; a programme lists
  spec and feature roots; each spec's outline leads to its sections; a
  feature exposes its implementing tasks and constraints; the plan shows
  execution state. Automated smell counts are hints, not a completeness
  or correctness certificate.

## How do I write or transfer a spec into the graph?

The writer procedure (from the kinetics conversion, 2026-11-05):

1. **Inventory** the claims, rulings, evidence, open decisions and tasks
   in the input — an existing graph, a supplied draft, or a readable
   legacy source. Never infer missing source content from its filename.
2. **Search for existing owners and read them before writing.** Map each
   claim to one canonical node; merge equivalent wording, preserve
   genuinely different scopes, expose unresolved conflicts. Record the
   original source in the audit/revision trail.
3. **Write each root** with its owner, ordered sections, dependencies
   and plan. Write sections as one design claim with observable
   acceptance and explicit unknowns. Search after a lost write receipt
   before retrying. Preserve human rulings and existing tasks. Assign one
   `part-of` parent, with `derived-from`/`cites` for evidence and
   `qualifies` for a limiting gotcha.
4. **Resolve placeholders** (cross-file and cross-spec handle gaps)
   before calling the transfer complete; a placeholder left in prose is
   an unresolved dependency, not a citation.
5. **Reuse the programme's plan tree.** Each slice serves its canonical
   section, states its acceptance once or explicitly names the generated
   snapshot, and records dependencies and unresolved decisions. A
   snapshot needs a source revision and refresh rule. Keep dispatch
   disabled unless separately authorized.
6. **Final pass.** Re-read bodies and links; roots fit the rendering
   limit; single-parent membership holds; no active file dependencies or
   stale current-state claims remain; verify processing before recall.

Multiple writers own **disjoint subtrees**; the **coordinator resolves
shared nodes** — one writer's placeholder in a shared body is the
coordinator's cross-file resolution pass, run before the transfer is
called complete. Scope-verification caveat: `gr621795` records a
reproduced tags-plus-`under` search leak; until its fix is verified,
establish membership from `part-of` links and inspect the actual returned
handles — a scoped banner alone is not evidence that every result belongs
to the subtree.

## How do I prune a transferred spec against the code?

The prune pass keeps the spec graph free of things that are already
built. Run it after a transfer and repeat it as code lands.

- **Classify every section** `BUILT`, `PARTIAL`, `OPEN` or `REFERENCE`
  against code **through the python kind** (`search(kind='python',
  mode='pattern', …)`, `get(kind='python', id='<alias>::<qualname>')`).
  Evidence rule: **no verified handle, no BUILT** — a section whose
  behaviour exists in code does not stay in the graph as design text.
- **Apply**: a holding hub per spec carries the prune results. While
  `gr569350` blocks deletion, a retire-pending section becomes a pointer
  node under it; hub bodies carry the code handles. A `PARTIAL` body
  opens with **one Built sentence** naming the built part and its
  handles, and keeps only its open design. Design absent in code gets
  **new slices** on the plan. Surveys and evidence stay as reference;
  gotchas stay (memory holds gotchas); rulings on shipped decisions stay
  as `absorbed`.
- **Verification caveat (`gr569538`)**: before trusting a BUILT verdict,
  check `get(kind='python', id='<alias>', view='provenance')` against
  the pyproject pin — a symbol that resolves against a stale engine is
  not evidence about the served code.

## How do I read a spec?

1. `get(kind='memory', id=<spec root>, extent='fisheye')` — the root
   body carries the ordered section or sub-hub handles.
2. Walk the handles **in the root body's reading order** (the `contains`
   ring is unordered); `get` each section. Use `fisheye+1hop` when you
   need a section's evidence and rulings ring; the extent ladder is
   [[precis-fisheye-help]].
3. For execution state, cross to the plan: `get(kind='todo', id=<plan
   root>, view='tree')` shows the slices, their STATUS, leases and
   `blocked-by` sequence.

## How do the slicer, the ready agent and a builder each consume a spec?

- **Slicer** — reads the spec root plus its sections, then cuts slice
  todos under the plan root: one slice per section or feature, each
  serving its feature node and pointing at the section's normative
  acceptance contract, sequenced with `blocked-by` edges
  (`precis-spec-help~6`).
- **Ready agent** — judges build-readiness against the section contract:
  a concrete trigger, expected result and acceptance evidence. Missing
  fixtures or thresholds must already be named decisions on the plan; if
  they are hidden assumptions, the spec is not ready and the ready agent
  says so instead of smoothing them over.
- **Builder** — reads its slice todo, follows it to the section node for
  the acceptance contract, and works from a self-contained brief (a
  named-revision snapshot) when the graph is unreachable. It records
  status, lease and `landed:` sha on the todo. It never edits spec prose
  to say "done" — retirement (`precis-spec-help~12`) is a separate step.

## How do I retire a file spec or a built section?

**File spec retired in favour of the graph spec.** The file is deleted
**in the landing commit** that ships the graph spec — the same commit,
so no interval exists where the pointer names a file that is already
gone. A `supersedes` edge runs **from the spec root to the old thread
node** so the file-era history stays reachable:

```python
link(kind="memory", id=<spec root id>, rel="supersedes",
     target="memory:<old thread node id>")
```

Keep prior text and source-path provenance in revision history; it is
not repeated as current instructions. Never infer a completed migration,
an implementation verdict, or a missing file's contents from a pointer
alone — the tooling prerequisite is tracked separately (`gr568171`), and
its completion is not proof of physical file deletion.

**Built section.** A section whose behaviour already exists in code
retires at once, superseded by the python-kind handle of the symbol that
carries it (`python:catpath::<qualname>` for the engine,
`python:main::<qualname>` for precis); its group hub or spec root keeps a
one-line pointer naming the behaviour and the handles. A partially built
section keeps only its open design, opening with one Built sentence.

**Lifecycle retirement.** When every slice todo serving a section is
done and its gripes are closed, the section retires — superseded by the
skill or docstring anchor carrying the surviving truth; the spec root
retires when its last section has. Retire with
`delete(kind='memory', id=<id>, args={'superseded_by': '<handle>'})` after
review and preservation of still-true claims (full recipe:
[[precis-delete-help]]); while `gr569350` blocks deletion, leave the
retire-pending pointer node under the spec's holding hub instead.

## See also

- [[precis-memory-help]] — hubs, part-of, rulings, retirement mechanics
- [[precis-fisheye-help]] — the extent ladder for reading
- [[precis-todo-tree-help]] — plan roots, slices, blocked-by, leases
- [[precis-python-help]] — verifying code handles through the python kind
- [[precis-work-help]] — claiming slices, the work view
