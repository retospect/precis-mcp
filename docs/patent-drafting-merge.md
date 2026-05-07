# Patent-drafting merge: bringing `patentorney-mcp` into precis

> Status: **proposal**. Decision: Option A (full seven-verb mapping) with
> engagement-triggered kind registration. Supersedes the brief Option D
> (side-channel domain tools) discussed earlier in the thread.

## TL;DR

`patentorney-mcp` becomes a sub-system inside `precis-mcp`. Its 8
domain tools collapse onto seven precis verbs over a family of new
kinds (`patent-claim`, `patent-figure`, …). The sub-kinds are
**hidden until the user engages with patent drafting**, at which
point a single `put(kind='patent', mode='init', …)` call:

1. Creates `${PRECIS_ROOT}/.patent.yaml`
2. Registers the patent-drafting sub-kinds for the rest of the session
3. Hint-bus surfaces "you can now draft claims, figures, numerals…"

This keeps `tools/list` minimal for non-patent users, preserves
precis's seven-verb purity, and gives patent users full domain
ergonomics. One MCP server, one config entry, one repo to maintain.

## Goals

- **One MCP endpoint, one wheel, one repo.** Retire
  `pips/packages/patentorney-mcp` after the merge.
- **No verb-model violation.** Every patent operation lands on
  `get / search / put / edit / delete / tag / link`. Domain
  bulk-ops use a new precis-wide `id='*'` sentinel (Section 5).
- **No tool-list pollution.** Non-patent users never see the
  drafting kinds. Existing `kind='patent'` (EPO OPS lookup) stays
  always-on — that already works for everyone.
- **Discoverability via skills.** Patent-drafting agent help ships
  as `precis-patent-help` + per-kind `precis-patent-<kind>-help`
  skills, surfaced through the new TOC + semantic skill search
  landed in May 2026 (commits `5bdd6b9` + `872ca8f`).

## Non-goals (this proposal)

- Cross-corpus linking (`prior_art doi=…` → `link target='paper:<slug>'`)
  is documented but **deferred to phase 4**. Phase 1-3 land without it.
- New verb introduction. The seven verbs stay.
- Replacing the existing read-only `kind='patent'` (EPO OPS lookup)
  in `docs/patent-kind-spec.md`. The new modes (`init`,
  `set-root`, `export-*`) **extend** that kind; the search/get
  surface for foreign patents is untouched.

---

## 1. Architecture: lazy kind families

### 1.1 Trigger mechanism

Two paths reach the same end-state — patent-drafting kinds become
visible in `tools/list` for the rest of the precis-mcp process:

**Boot-time trigger.** If `${PRECIS_ROOT}/.patent.yaml` exists
when `dispatch.boot()` runs, the patent-drafting handlers register
just like markdown/plaintext/tex do today (see
`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/dispatch.py:560-570`).
Existing projects "just work" after restart.

**Engagement-time trigger.** If no `.patent.yaml` exists, only
`kind='patent'` (the OPS lookup kind) is visible. The agent calls
`put(kind='patent', mode='init', id='<project-slug>')`. The handler:

1. Validates `precis_root` is configured
2. Creates `${PRECIS_ROOT}/.patent.yaml` with a stub structure
3. Calls `hub.register_handler(...)` for each drafting sub-kind
4. Emits a hint via `hub.emit_hint(...)` listing the newly-available
   verbs

The MCP protocol fires `notifications/tools/list_changed` so clients
refresh their tool list. (FastMCP's `Tool.add` already triggers this;
we just call it from inside the handler.)

### 1.2 Why engagement-triggered (not always-on)

- **Cold token cost.** Every kind contributes ~3-5 lines to
  `tools/list` and ~200-400 tokens to the `_INSTRUCTIONS` block
  agents see at handshake. Six new kinds × hundreds of agents that
  never touch patents = real waste.
- **Mental-load cost.** A 7B agent staring at 23 kinds reasons
  worse than one staring at 17.
- **Discoverability is preserved.** The skill corpus's
  `precis-patent-help` is always visible (skills aren't gated). An
  agent searching skills for "patent" finds the help, which tells
  it to call `put(kind='patent', mode='init', …)` first.

### 1.3 Why a hidden file (`.patent.yaml`)

`.` prefix follows the same convention as `.envrc`, `.gitignore`,
`.precis-config`. It signals "tooling-managed; humans usually
shouldn't hand-edit". Doesn't appear in `ls` by default. Multi-project
users still address it explicitly via
`put(kind='patent', mode='set-root', text='/path/to/project')`.

---

## 2. Kind catalogue

Six new sub-kinds plus mode extensions to the existing `patent` kind:

| Kind | Numbering | Owns | Notes |
|---|---|---|---|
| `patent` *(extended)* | n/a | project lifecycle | Adds `mode='init'`, `mode='set-root'`, `mode='export-*'`, plus `view='status'`/`'check'`/`'tree'`/`'ids-check'`. Search/get for OPS-lookup unchanged. |
| `patent-claim` | numeric pos (1, 2, 3…) | claim text + structured elements | Slug-addressed; presentation number computed from list order. |
| `patent-figure` | numeric pos (FIG. 1…) | figure metadata + numerals_shown | Slug-addressed. |
| `patent-numeral` | series (100, 110…) | reference numerals | Slug-addressed; series derived from owning figure. |
| `patent-prior-art` | n/a | citations | Slug-addressed. Cross-link to `paper:<slug>` is opt-in (phase 4). |
| `patent-ids-submission` | numeric (auto) | IDS filings | Numeric-ref kind (like `memory`/`todo`). Each submission references `patent-prior-art` slugs. |
| `patent-term` | n/a | glossary entries | Slug-addressed (lowercase term). |

### 2.1 Kind-name policy precedent

This proposal is the **first multi-word kind family** in precis
(every existing kind is a single word: `paper`, `oracle`, `random`,
`youtube`, etc.). Naming rule going forward:

> Kind names use lowercase ASCII alphanumerics + hyphens, matching
> `^[a-z0-9][a-z0-9-]*$`. Domain families use a domain prefix
> (`patent-`, future `legal-`, `clinical-`) followed by an entity
> name. Single-word kinds remain bare.

Add this rule to `docs/file-kinds-unified-addressing.md`.

### 2.2 Why `patent-ids-submission` is its own kind

The existing patentorney `prior_art` tool conflates two things:
prior-art records (one per cited document) and IDS submissions
(filings to the USPTO that list which prior art was disclosed and
when). They have distinct lifecycles, distinct addressing, and
agents need to query them differently ("what have I cited?" vs
"what have I disclosed?"). Splitting them into two kinds is
*more* honest than the original tool surface.

---

## 3. Verb mapping (full table)

| Patentorney action | Precis call | Notes |
|---|---|---|
| `set_root(path)` | `put(kind='patent', mode='set-root', text=path)` | Mutates per-process state. |
| `guide(topic)` | `search(kind='skill', q=topic)` | Removed — TOC + semantic skill search supersedes it. |
| `claim("add", id, …)` | `put(kind='patent-claim', id=…, text=<payload>)` | Structured payload — see §4. |
| `claim("get", id)` | `get(kind='patent-claim', id=…)` | |
| `claim("update", id, …)` | `edit(kind='patent-claim', id=…, mode='replace', text=…)` | |
| `claim("remove", id)` | `delete(kind='patent-claim', id=…)` | |
| `claim("move", id, after)` | `edit(kind='patent-claim', id=…, mode='reorder', after=…)` | First concrete consumer of `mode='reorder'` (declared in protocol, deferred today). |
| `claim("rename", old, new)` | `edit(kind='patent-claim', id=old, mode='rename', text=new)` | |
| `claim("tree")` | `get(kind='patent', view='tree')` | Project-level rollup. |
| `figure("list")` | `get(kind='patent-figure')` | Bare-id index view (precis convention). |
| `figure("add" / "get" / "update" / "remove")` | `put / get / edit / delete (kind='patent-figure', …)` | Same shape as claim. |
| `figure("move", id, position)` | `edit(kind='patent-figure', id=…, mode='reorder', after=…)` | Triggers cascading numeral-series renumber as a side-effect (§5). |
| `figure("rename", old, new)` | `edit(kind='patent-figure', id=old, mode='rename', text=new)` | |
| `numeral("add" / "get" / "update" / "remove")` | `put / get / edit / delete (kind='patent-numeral', …)` | |
| `numeral("renumber")` | `edit(kind='patent-numeral', id='*', mode='renumber')` | Bulk; uses precis-wide `id='*'` sentinel (§5). |
| `numeral("rename", old, new)` | `edit(kind='patent-numeral', id=old, mode='rename', text=new)` | |
| `numeral("lookup", label)` | `search(kind='patent-numeral', q=label)` | |
| `numeral("list")` | `get(kind='patent-numeral')` | |
| `prior_art("add" / "get" / "update" / "remove" / "list")` | `put / get / edit / delete / get-bare (kind='patent-prior-art', …)` | |
| `prior_art("ids_add", date, refs)` | `put(kind='patent-ids-submission', text=…)` | New kind — see §2.2. |
| `prior_art("ids_list")` | `get(kind='patent-ids-submission')` | |
| `prior_art("ids_check")` | `get(kind='patent', view='ids-check')` | Project-level check; reuses existing validator. |
| `glossary("add" / "get" / "update" / "remove" / "list")` | `put / get / edit / delete / get-bare (kind='patent-term', …)` | |
| `export("status")` | `get(kind='patent', view='status')` | Read-only computation. |
| `export("check")` | `get(kind='patent', view='check')` | Runs `validators.run_checks`. |
| `export("claims")` | `get(kind='patent-claim', view='text')` | Plain rendered claims. |
| `export("drawings_description")` | `get(kind='patent-figure', view='description')` | Plain rendered drawings description. |
| `export("claims_latex")` | `put(kind='patent', mode='export-claims-latex')` | Writes `sections/claims.tex`. |
| `export("drawings_latex")` | `put(kind='patent', mode='export-drawings-latex')` | Writes `sections/drawings-description.tex`. |
| `export("latex")` | `put(kind='patent', mode='export-latex')` | Writes both. |

**Coverage**: every patentorney action maps. Three patterns appear:

1. **Standard CRUD** — `put / get / edit / delete` per kind.
2. **Project-level views** — `view='status'`, `view='check'`,
   `view='tree'`, `view='ids-check'` on `kind='patent'`.
3. **Codegen on `kind='patent'`** — `mode='export-*'` writes
   generated files. See §6.

---

## 4. The structured payload question

`claim("add", elements=…)` takes a structured payload (preamble +
transitional + list of elements, each with text + numeral
associations). Precis's `put` is fundamentally text-shaped.

Three options for representing structured claims:

### 4.1 JSON in `text=` (recommended)

```python
put(kind='patent-claim', id='c1', text=json.dumps({
    "preamble": "A method for synthesizing a metal-organic framework",
    "transitional": "comprising",
    "elements": [
        {"text": "providing a metal salt and an organic ligand",
         "numerals": ["100", "110"]},
        {"text": "dissolving them in a solvent",
         "numerals": ["120"]},
    ],
}))
```

Pros: matches precis's text discipline; `get(kind='patent-claim', id='c1')`
can return either rendered prose (default view) or the JSON
(`view='raw'`). Round-trippable.

Cons: agents have to construct JSON. But they already do this for
similar use cases (`fc` flashcard payloads).

### 4.2 Markdown convention

```python
put(kind='patent-claim', id='c1', text="""
# preamble
A method for synthesizing a metal-organic framework

# transitional
comprising

# elements
- providing a metal salt and an organic ligand [100, 110]
- dissolving them in a solvent [120]
""")
```

Pros: more human-readable on round-trip.

Cons: parsing is fragile; bracket syntax for numerals is
non-obvious; the patent kind handler ends up shipping a custom
mini-DSL parser. Reject.

### 4.3 Mode-keyed putters

```python
put(kind='patent-claim', mode='add-element', id='c1', text='dissolving them...')
edit(kind='patent-claim', mode='set-preamble', id='c1', text='A method...')
```

Pros: each call is simple.

Cons: turns a single conceptual action ("add a claim") into 5+
calls. Worst of both worlds. Reject.

**Decision: 4.1 (JSON in `text=`).** Helper view `view='example'`
on the kind returns a copy-pasteable skeleton.

---

## 5. The `id='*'` bulk sentinel

### 5.1 Motivation

`numeral("renumber")` is genuinely a bulk operation — it touches
every numeral in the project, not one. Forcing it through a per-id
loop loses atomicity (the renumber needs to be transactional or
the project is left in a half-state). A precis-wide bulk-edit
sentinel solves this cleanly:

```python
edit(kind='patent-numeral', id='*', mode='renumber')
```

### 5.2 Specification

Add to `docs/edit-protocol-spec.md`:

> When `id='*'` is supplied to `edit` or `delete`, the call is a
> **bulk operation** over every ref of the kind that matches the
> implicit scope (or an explicit `tags=`/`scope=` filter). Each
> kind opts in via `KindSpec.supports_bulk: bool = False`. Kinds
> that don't opt in raise `[error:Unsupported]` with a hint to
> enumerate refs first.

### 5.3 Per-kind opt-in

Most kinds keep `supports_bulk=False`. Documenting:

| Kind | `supports_bulk` | Why |
|---|---|---|
| `patent-numeral` | True | Renumber-by-figure-order is the canonical case. |
| `patent-claim` | False | Move/reorder is per-claim; bulk delete is too footgun-y. Opt in later if a real use case emerges. |
| `patent-figure` | False | Same reasoning. |
| `memory`, `todo`, `gripe` | False | Numeric-ref kinds enumerate cheaply via `search`; no bulk verbs needed. |
| `paper`, `patent` *(OPS)* | False | These are read-only as far as content; bulk delete would orphan blocks. |
| `markdown`, `plaintext`, `tex` | False | File operations are per-file. Bulk delete = "rm -rf". Footgun. |

### 5.4 Modes available under `id='*'`

A bulk-supporting kind declares which modes accept the sentinel.
For `patent-numeral`:

- `mode='renumber'` — recompute series from figure order
- `mode='delete'` (via `delete(kind='patent-numeral', id='*', tags=['orphaned'])`)
  — bulk delete of tagged subset

Per-kind modes documented in the kind's help skill. The protocol
treats `id='*'` as a generic flag; semantics belong to the handler.

### 5.5 Safety rails

- `id='*'` without `tags=` or `scope=` against a non-bulk-aware kind
  raises `[error:Unsupported]`.
- `id='*'` with `dry_run=True` (existing `edit` kwarg) returns the
  predicted change set without writing — strongly recommended in
  the help skill.
- The advisory-lock pattern from oracle_sync (`pg_try_advisory_lock`)
  applies to bulk operations on store-backed kinds.

---

## 6. The `mode='export-*'` codegen convention

Patentorney's `export("latex")` writes generated `.tex` files into
`sections/`. This is filesystem mutation, so it's `put` (not `get`).
But it's not "create a new ref" either. Two existing precis patterns
overlap:

- File kinds (`markdown` / `plaintext` / `tex`) use `put(mode='create')`
  to write files.
- The new oracle_sync code uses `put` semantics implicitly for
  store-side state mutations.

Generalising: **`mode='export-*'` is the codegen verb on
project-owning kinds**. The convention:

- Lives on the project root kind (here: `kind='patent'`).
- Reads from the project's store (or `.patent.yaml` here) and
  writes generated files into the project tree.
- Is idempotent — re-running with the same source produces the
  same output.
- Returns a manifest in the response body listing what was
  written.

Document this in `docs/edit-protocol-spec.md` so the next
project-owning kind (lab-mcp's `experiment`?) can reuse it.

---

## 7. File / module layout

```
src/precis/
├── handlers/
│   ├── patent.py                     # extended: + mode='init', 'set-root', 'export-*', + view='status', 'check', 'tree', 'ids-check'
│   ├── patent_claim.py               # PatentClaimHandler — thin shim over precis.patent.models
│   ├── patent_figure.py
│   ├── patent_numeral.py
│   ├── patent_prior_art.py
│   ├── patent_ids_submission.py
│   └── patent_term.py
├── patent/                           # back-end library; not touched by the seven-verb dispatch directly
│   ├── __init__.py
│   ├── models.py                     # ← from patentorney_mcp/models.py (verbatim)
│   ├── validators.py                 # ← from patentorney_mcp/validators.py
│   ├── exporters.py                  # ← extracted from patentorney_mcp/utils.py
│   ├── store.py                      # PatentTransaction, file-locking, .patent.yaml I/O
│   └── dispatch.py                   # register_drafting_kinds(hub) — called from boot() or from PatentHandler.put(mode='init')
└── data/skills/
    ├── precis-patent-help.md         # entry skill — covers init flow + sub-kind menu
    ├── precis-patent-claim-help.md
    ├── precis-patent-figure-help.md
    ├── precis-patent-numeral-help.md
    ├── precis-patent-prior-art-help.md
    ├── precis-patent-ids-submission-help.md
    └── precis-patent-term-help.md
```

### 7.1 Handler shape (representative)

```python
# src/precis/handlers/patent_claim.py
class PatentClaimHandler(Handler):
    spec = KindSpec(
        kind="patent-claim",
        title="Patent Claim",
        description=(
            "A claim within the active patent project. Enabled when "
            "${PRECIS_ROOT}/.patent.yaml exists."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=False,
        supports_link=True,
        supports_bulk=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        # Lazy import keeps the patent back-end out of cold-start
        # when the kind isn't registered. The hub mutation that
        # triggers init() guarantees .patent.yaml exists by the
        # time we run.
        from precis.patent.store import PatentTransaction
        self._tx_class = PatentTransaction

    def put(self, *, id, text, mode=None, **kw):
        # Parse JSON payload → ClaimBody → write through transaction.
        ...
```

Each sub-handler is ~150-300 LOC: argument validation, payload
parsing, delegation to `precis.patent.models` / `validators`, hint
emission. The heavy lifting (data model, validation, file locking,
LaTeX export) stays in `precis.patent.*` libraries.

---

## 8. Skill-discovery integration

Every kind ships its help skill. The TOC + semantic skill search
landed in May 2026 (`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/skill.py:209-279`)
makes them findable:

```python
search(kind='skill', q='draft a patent claim')
# → precis-patent-claim-help (semantic top hit, score ~0.6)

search(kind='skill', q='renumber reference numerals')
# → precis-patent-numeral-help

get(kind='skill', id='precis-patent-help')
# → entry document: init flow, sub-kind menu, JSON payload examples
```

The drafting kinds appear in `get(kind='skill', id='toc')` regardless
of whether `.patent.yaml` exists, with an `[unwired]` marker (the
existing `_availability_gap` mechanism in `skill.py:271-277`) when
the project hasn't been initialised. Cold-start agents see them in
the TOC, click through, follow the init flow.

---

## 9. Cross-corpus links (deferred to phase 4)

`prior_art add citation=… doi='10.1234/…'` could automatically
write `link(kind='patent-prior-art', id=…, target='paper:<slug>')`
when the paper exists in precis's store, or fetch-then-link when
it doesn't (paper kind already supports DOI-as-id since
`f241d40`). Cross-kind search then reaches into patent prior-art
records:

```python
search(q='photocatalytic NOx reduction')
# Cross-kind fan-out includes patent-prior-art entries that cite
# papers matching the query.
```

Keep this opt-in via a config flag (`patent.cross_link_papers: bool`)
because not every patent project wants its prior-art records
showing up in unrelated `paper` searches. Defer until phase 4.

---

## 10. Sequencing

Each phase commits independently and leaves `pytest` green.

### Phase 1 — back-end library copy-in

- Copy `patentorney_mcp/{models,utils,validators}.py` → `precis/patent/`
- Rename module-internal imports
- Add `precis/patent/store.py` consolidating file-lock + transaction code
- No MCP wiring, no boot changes
- Tests: port `patentorney-mcp/tests/` → `precis-mcp/tests/test_patent_*.py`
- **Definition of done**: 1801 + N existing tests pass; new
  `tests/test_patent_models.py` etc. pass; no kind appears in
  `tools/list` yet.

### Phase 2 — extend `kind='patent'`

- Add `mode='init'`, `mode='set-root'` to existing `PatentHandler`
- Add `view='status'`, `view='check'`, `view='tree'`, `view='ids-check'`
- Wire `register_drafting_kinds(hub)` helper that does the
  in-process kind registration
- Boot-time: if `${PRECIS_ROOT}/.patent.yaml` exists, call the
  helper from `dispatch.boot()`
- Engagement-time: `put(kind='patent', mode='init')` calls it
  after creating the file
- Tests: end-to-end init flow; idempotency; multi-project via
  `set-root`
- **Definition of done**: agents can run the init flow; sub-kinds
  appear in `tools/list` mid-session.

### Phase 3 — the six drafting handlers

- One handler per kind: `patent-claim`, `patent-figure`,
  `patent-numeral`, `patent-prior-art`, `patent-ids-submission`,
  `patent-term`
- Implement `mode='reorder'` for `edit` (was deferred in protocol
  docstring; patent kinds are the first consumer)
- Implement `id='*'` bulk sentinel + per-kind `supports_bulk`
  flag in `KindSpec`
- Implement `mode='export-*'` for `kind='patent'` (writes
  `sections/*.tex`)
- Tests: every action from §3's mapping table has a passing test
- **Definition of done**: every patentorney use case works
  through precis verbs.

### Phase 4 — skills + cross-link + properly retire

**Skill authoring**

- Author seven help skills under `data/skills/precis-patent-*-help.md`
- Verify TOC + semantic skill search surface them (run live
  bge-m3 sanity check as in Phase B of the May 2026 work)

**Cross-corpus link**

- Implement opt-in `prior_art` → `paper:<slug>` link

**Pip retirement (proper, not `rm -rf`)**

- **PyPI**: ship a final `patentorney-mcp` v0.3.0 to PyPI whose
  package is a single deprecation stub:
  - `__main__.py` prints "patentorney-mcp has been merged into
    precis-mcp; install `precis-mcp` and configure
    `${PRECIS_ROOT}/.patent.yaml`. See
    https://github.com/.../precis-mcp/blob/main/docs/patent-drafting-merge.md"
    then exits 0
  - `README.md` mirrors the same notice with the migration
    cookbook (mapping from old MCP tool calls to new precis verb
    calls; basically §3 of this doc rendered for end-users)
  - `pyproject.toml` declares `precis-mcp` as a runtime
    dependency so `pip install patentorney-mcp` pulls in the
    real package
  - Tag the v0.3.0 release with a `Deprecated:` line in the
    GitHub release notes; pin to it for one full minor release
    cycle of precis-mcp before yanking older versions
- **Workspace cleanup**:
  - Delete `pips/packages/patentorney-mcp/` from the monorepo
  - Remove from `pips/manifest.yml`
  - `grep -r 'patentorney-mcp' .` and update every reference:
    workspace `README.md`, any Windsurf `.mcp.json` configs,
    any sortie scripts that referenced the old MCP tools
  - Remove the package from `uv.lock` workspace member list
- **CI / release plumbing**:
  - Update `/release` workflow to no longer release patentorney
  - Update `/patrol` to skip patentorney's CI checks
- **User comms**:
  - Note in next precis-mcp release notes:
    > **Migrated**: patentorney-mcp's drafting tools now live
    > inside precis-mcp under `kind='patent'` and the
    > `patent-*` family. Existing `patent.yaml` files are
    > read; rename to `.patent.yaml` to use the new
    > engagement trigger. See
    > docs/patent-drafting-merge.md.

**Definition of done**

- `pips/packages/patentorney-mcp/` directory gone from the
  monorepo
- Final v0.3.0 stub published to PyPI with deprecation notice
- No references to `patentorney-mcp` remaining anywhere in the
  workspace except the migration note in release notes
- One MCP package (`precis-mcp`) covers the full surface
- Skill search round-trips on every `precis-patent-*-help` skill
- Existing patentorney users following the README upgrade
  cookbook can migrate without writing custom scripts

---

## 11. Open risks and mitigations

| Risk | Mitigation |
|---|---|
| Tool-list churn confuses MCP clients that don't handle `notifications/tools/list_changed` cleanly | FastMCP already emits this; document the expected behaviour in the help skill so users know to restart their client if it's stuck. |
| `id='*'` becomes a slippery slope — every kind starts asking for it | Per-kind opt-in plus the safety-rails in §5.5. Document the rule: *opt in only when bulk is the natural shape, not as a shortcut for "loop over results"*. |
| Exposing `mode='export-latex'` from `kind='patent'` blurs the kind/project boundary | Document `mode='export-*'` as the codegen convention (§6) — it lives on project-owning kinds explicitly, not as a generic verb. |
| Structured-payload JSON is awkward for 7B agents | Ship a `view='example'` skeleton on every payload-taking kind so the agent can `get` an example, edit, and `put` it back. |
| Patent-drafting users without precis_root configured can't do anything | Boot-time check: if patent kinds register via boot trigger but `precis_root` is unset, raise InitError with a hint. Engagement trigger always validates `precis_root` first. |
| Two `kind='patent'` overloads (OPS lookup + drafting) confuse agents | The skill `precis-patent-help` opens with the disambiguation: search/get-on-id = lookup; init/set-root/export = drafting. |

---

## 12. Decisions taken (vs. alternatives)

- **Engagement-triggered registration** over always-on (Section 1.2)
- **`patent-claim` over `pat-claim`** — full prefix wins on clarity
  per user direction
- **`id='*'` bulk sentinel** with per-kind opt-in over
  implicit-cascade-only
- **JSON-in-`text=`** for structured claim payloads over
  markdown DSL or mode-keyed putters
- **`mode='export-*'` on `kind='patent'`** over a generic
  `kind='export'` or a side-channel CLI tool
- **One MCP wheel**, retire `patentorney-mcp` after phase 4 — full
  consolidation per user direction
