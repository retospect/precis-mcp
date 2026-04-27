# `pycode` — Python codebase navigation kind

> Status: **draft spec**. Not yet scheduled. Sized as a single phase
> after phase 5 (state kinds), or sooner if it unblocks agent
> productivity on the cluster's own pip packages.

## Why

Agents currently reach for `find` + `grep` + `read_file` to orient
themselves in a Python repo. That works at small scale but burns
~40-80% of context on noise: full file dumps, repeated imports, test
boilerplate. There is no rooted, drillable map.

Aider's repo-map is the closest prior art (tree-sitter + PageRank →
ranked symbol summary), but it is not entry-point-rooted and is bundled
inside Aider. Nuanced CodeGraph is the only project shipping
"code graph as MCP", and it is young + library-shaped, not addressable.

A precis kind fits the gap exactly: refs are repos, blocks are files /
symbols, views give outline / call-graph / runtime overlays, addressing
gives drill-down. Same mental model as `paper`, different corpus.

## Surface (4 verbs)

```python
# Top-level: a registered repo
get(kind='pycode', id='precis-mcp-new')                  # repo overview
get(kind='pycode', id='precis-mcp-new', view='toc')      # package tree
get(kind='pycode', id='precis-mcp-new', view='entries')  # CLI/scripts/__main__

# Drill into a file
get(kind='pycode', id='precis-mcp-new/src/precis/registry.py')
get(kind='pycode', id='precis-mcp-new/src/precis/registry.py', view='outline')
get(kind='pycode', id='precis-mcp-new/src/precis/registry.py', view='source')
get(kind='pycode', id='precis-mcp-new/src/precis/registry.py~42-100')

# Drill into a symbol (slug = qualified dotted path)
get(kind='pycode', id='precis-mcp-new::precis.registry.Registry')
get(kind='pycode', id='precis-mcp-new::precis.registry.Registry.get')

# Composition from an entry point
get(kind='pycode', id='precis-mcp-new', view='callgraph',
    entry='precis.cli:main')
get(kind='pycode', id='precis-mcp-new', view='callgraph',
    entry='precis.cli:main', depth=3)

# Runtime overlay (opt-in; runs the code under sys.setprofile)
get(kind='pycode', id='precis-mcp-new', view='runtrace',
    entry='precis.cli:main', argv=['--help'])

# Search
search(kind='pycode', q='attribution footer rendering', scope='precis-mcp-new')
search(kind='pycode', q='cache TTL handling',
       scope='precis-mcp-new::precis.handlers')

# Register / unregister a repo
put(kind='pycode', id='precis-mcp-new',
    text='/Users/bots/Documents/.../precis-mcp-new', mode='register')
put(kind='pycode', id='precis-mcp-new', mode='reindex')
put(kind='pycode', id='precis-mcp-new', mode='unregister')
```

### KindSpec

```python
KindSpec(
    kind='pycode', title='Python code navigator',
    supports_get=True, supports_search=True, supports_put=True,
    is_numeric=False, id_required=True,
    views=('toc', 'entries', 'outline', 'source', 'callgraph',
           'runtrace', 'imports', 'symbols'),
    modes=('register', 'reindex', 'unregister'),
    requires_env=(),  # local; no API keys
)
```

## Addressing grammar

```
<repo>                                  repo overview
<repo>/<path/to/file.py>                file
<repo>/<path/to/file.py>~A-B            line range in file
<repo>::<dotted.qualified.symbol>       symbol (class / function / method)
<repo>::<module.path>/                  package or module
```

The `::` separator avoids confusion with filesystem `/`. Symbols
resolve via the repo's pre-built symbol index; ambiguous shortnames
return a disambiguation list (precis-style "options=" hint).

## Data model

One **ref per repo** in a new `pycode` corpus. Repo path stored in
`refs.meta.path`. Indexing is a side-table `pycode_symbols`:

```sql
create table pycode_symbols (
  ref_id      bigint not null references refs(id) on delete cascade,
  qualname    text not null,         -- e.g. precis.registry.Registry.get
  kind        text not null,         -- module|class|function|method
  file        text not null,         -- relative to repo root
  start_line  int  not null,
  end_line    int  not null,
  parent      text,                  -- enclosing qualname (or null)
  signature   text,                  -- "def get(self, kind: str) -> Handler"
  docstring   text,
  meta        jsonb not null default '{}',
  primary key (ref_id, qualname)
);
create index on pycode_symbols (ref_id, file);
create index on pycode_symbols (ref_id, parent);
create index on pycode_symbols using gin (to_tsvector('english',
  coalesce(docstring, '') || ' ' || qualname));
```

A second table `pycode_calls` for static call edges:

```sql
create table pycode_calls (
  ref_id    bigint not null references refs(id) on delete cascade,
  caller    text not null,           -- qualname
  callee    text not null,           -- qualname (or "ext:<name>" for unresolved)
  file      text not null,
  line      int  not null,
  primary key (ref_id, caller, callee, file, line)
);
create index on pycode_calls (ref_id, caller);
create index on pycode_calls (ref_id, callee);
```

Both populated by the indexer on `mode='register'` and `mode='reindex'`.
Re-index is incremental by file mtime.

## Indexing pipeline

1. **Walk** repo respecting `.gitignore` (use `pathspec`).
2. **Parse** each `.py` file with `ast.parse`. No tree-sitter dep
   for v1 — stdlib `ast` is fast enough (~1ms/file) and zero-cost.
3. **Outline pass** — collect modules, classes, functions, methods,
   docstrings, signatures, line ranges → `pycode_symbols`.
4. **Import pass** — record `import x` and `from a.b import c` per
   module → name-resolution table (in-memory, used by step 5).
5. **Call pass** — for each `ast.Call` node, attempt static
   resolution to a qualname using:
   - local scope (function locals are ignored — too noisy)
   - module-level names + imports
   - class MRO for method calls on `self`
   - fallback `ext:<name>` for unresolved (stdlib, third-party, dynamic)
   Edges → `pycode_calls`.
6. **Embed** outline rows (`qualname + signature + docstring`)
   into `blocks.embedding` for semantic `search`. One block per symbol.

This is **pyan-class static analysis** done with stdlib only. We
skip duck-typed dispatch on purpose — it is the least useful 5% and
costs the most.

### When `ast` is not enough

Hooks for upgrades, behind feature flags, deferred:

- **`tree-sitter-python`** — only if multi-language support is added
  later (Rust, JS). Add `pycode` → `code` rename.
- **`jedi` / `pyright`** — for cross-file type-aware resolution.
  Slower; gates behind `requires_env=('PYCODE_TYPED',)`.

## Views

### `toc` — package tree

```
# precis-mcp-new — TOC (24 modules, 7 packages)

  precis/                              package
  ├── __init__.py                       1 import
  ├── cli.py                          12 fn
  ├── config.py                        3 fn
  ├── embedder.py                      2 cls, 8 fn
  ├── errors.py                        6 cls
  ├── handlers/                        package
  │   ├── _cache_base.py               1 cls, 4 fn
  │   ├── calc.py                      1 cls
  │   ├── math.py                      1 cls, 6 fn
  │   ├── memory.py                    1 cls
  │   ├── paper.py                     1 cls, 14 fn
  │   ├── perplexity.py                4 cls, 9 fn
  │   ├── web.py                       1 cls, 5 fn
  │   └── youtube.py                   1 cls, 4 fn
  ├── ingest.py                        9 fn
  ├── protocol.py                      1 dataclass, 1 cls
  ├── registry.py                      1 cls, 1 fn
  ├── runtime.py                       2 cls, 3 fn
  ├── server.py                        1 cls, 5 fn
  └── store/                           package

Next:
  get(kind='pycode', id='precis-mcp-new', view='entries')
  get(kind='pycode', id='precis-mcp-new::precis.registry')
  get(kind='pycode', id='precis-mcp-new', view='callgraph',
      entry='precis.cli:main')
```

### `entries` — runnable entry points

Reads `pyproject.toml` (`[project.scripts]`, `[project.entry-points]`),
finds `if __name__ == "__main__":` guards, and any `argparse.ArgumentParser()`
call sites.

```
# precis-mcp-new — entry points

  Console scripts:
    precis            → precis.cli:main         (registered)
    precis-paper      → precis.cli:paper_main   (registered)

  __main__ guards:
    src/precis/server.py:142    runs `serve()`
    src/precis/ingest.py:284    debug bulk-ingest

Next:
  get(kind='pycode', id='precis-mcp-new', view='callgraph',
      entry='precis.cli:main')
```

### `outline` — class/method TOC per file

```
# src/precis/registry.py — outline

  IMPORTS
    precis.errors.NotFound
    precis.embedder.Embedder        (TYPE_CHECKING)
    precis.protocol.Handler         (TYPE_CHECKING)
    precis.store.Store              (TYPE_CHECKING)

  FUNCTIONS
    L21  builtins(*, store=None, embedder=None) -> list[Handler]
         "Return handler instances for the active server configuration."

  CLASSES
    L103 Registry
         "Resolves a `kind=` string to a handler instance."
         L111  __init__(self, handlers)
         L120  get(self, kind) -> Handler
         L130  kinds(self) -> list[str]
         L133  __contains__(self, kind) -> bool
         L136  __len__(self) -> int

Next:
  get(kind='pycode', id='precis-mcp-new::precis.registry.Registry')
  get(kind='pycode', id='precis-mcp-new/src/precis/registry.py~21-100',
      view='source')
```

### `outline` on a symbol — drill-down

```python
get(kind='pycode', id='precis-mcp-new::precis.registry.Registry')
```

```
# precis.registry.Registry  (class, src/precis/registry.py:103-137)

"Resolves a `kind=` string to a handler instance.

Unavailable kinds (KindSpec.requires_env not satisfied) are silently
omitted at construction time."

Methods:
  __init__(self, handlers)                      L111
  get(self, kind) -> Handler                    L120  raises NotFound
  kinds(self) -> list[str]                      L130
  __contains__(self, kind) -> bool              L133
  __len__(self) -> int                          L136

Calls (from this class):
  precis.errors.NotFound.__init__               1× (in get)

Called by:
  precis.runtime.build_runtime                  1×
  precis.server.PrecisServer._dispatch          3×

Next:
  get(...::Registry.get, view='source')
  get(...::precis.runtime.build_runtime)
```

### `source` — the actual code

`source` returns the full source for a symbol or file range. This
exists so that after an agent has navigated to the right place, it
can read code without falling back to `read_file`.

### `callgraph` — entry-point-rooted static graph

The flagship view. Tree (not graphviz) rooted at `entry=`.

```python
get(kind='pycode', id='precis-mcp-new', view='callgraph',
    entry='precis.cli:main', depth=3)
```

```
# Static call graph from precis.cli:main  (depth=3)

precis.cli.main
├── precis.cli._build_parser
│   └── argparse.ArgumentParser                              [ext]
├── precis.cli._cmd_serve
│   ├── precis.runtime.build_runtime
│   │   ├── precis.config.Config.from_env
│   │   ├── precis.store.Store
│   │   ├── precis.embedder.build_embedder
│   │   └── precis.registry.builtins
│   └── precis.server.PrecisServer.run
│       └── mcp.server.fastmcp.FastMCP.run                   [ext]
├── precis.cli._cmd_ingest
│   ├── precis.runtime.build_runtime  …                      (see above)
│   └── precis.ingest.ingest_bundle
│       ├── precis.ingest._read_acatome
│       ├── precis.ingest._upsert_paper
│       └── precis.ingest._embed_blocks
└── precis.cli._cmd_serve_dev …                              (truncated, depth)

Legend:
  [ext]  unresolved (stdlib / third-party / dynamic)
  …      truncated by depth or already shown above

Notes:
  3 dynamic dispatches not resolved statically:
    precis.server.PrecisServer._dispatch  → handler.{get|search|put|move}
    precis.handlers.paper.PaperHandler._render_view → view-keyed branch
  Use view='runtrace' to capture them.

Next:
  get(...view='callgraph', entry='precis.cli:main', depth=5)
  get(...view='runtrace', entry='precis.cli:main', argv=['serve'])
  get(...::precis.runtime.build_runtime)
```

`depth` defaults to 3. Cycle detection collapses revisits to `…`.

### `runtrace` — dynamic overlay

Opt-in. Runs the entry point in a subprocess under `sys.setprofile`
(or `viztracer` if installed), captures the call sequence with hit
counts and elapsed time, and overlays it on the static graph.

```python
get(kind='pycode', id='precis-mcp-new', view='runtrace',
    entry='precis.cli:main', argv=['--version'])
```

```
# Runtime trace of precis.cli:main --version  (47 calls, 18ms)

precis.cli.main                              1×    18.0ms
└── precis.cli._build_parser                  1×    16.4ms
    ├── argparse.ArgumentParser.__init__     1×    14.1ms  [ext]
    └── argparse.ArgumentParser.add_argument 23×    2.1ms  [ext]

Static-only (not exercised this run):
  precis.cli._cmd_serve, precis.cli._cmd_ingest,
  precis.runtime.build_runtime, …

Next:
  get(...view='runtrace', entry='precis.cli:main', argv=['serve'])
  get(...view='callgraph', entry='precis.cli:main')
```

Sandboxing: `runtrace` is **gated by an env var** (`PYCODE_ALLOW_EXEC=1`)
because it executes user code. Default off. Document loudly.

### `imports`, `symbols` — flat helper views

- `imports` — flat dependency map (module → set of imports), useful
  for "what does this depend on" agent queries.
- `symbols` — flat list of all symbols, paginated; for completion-
  style search.

## Search

`search(kind='pycode', q=…)` does hybrid search over the symbol
index:
- **lexical** on `qualname || signature || docstring` (pg_trgm /
  to_tsvector)
- **semantic** on the per-symbol embedding (one block per symbol)

`scope=` accepts repo, package, or file:
```python
search(kind='pycode', q='cache attribution', scope='precis-mcp-new')
search(kind='pycode', q='cache attribution',
       scope='precis-mcp-new::precis.handlers')
search(kind='pycode', q='cache attribution',
       scope='precis-mcp-new/src/precis/handlers/_cache_base.py')
```

## Git integration

The symbol index already knows `(file, start_line, end_line)` for every
qualname, so mapping any symbol to its git history is nearly free. Git
becomes first-class in `pycode`, plus a small sibling `git` kind for
repo-scoped questions that don't need symbols.

### Library choice

Shell out to `git` via `subprocess` for v1. Zero deps, every cluster
node has git, same pattern the existing `code-repo-mcp` uses. Wrap
behind `pycode_index/git.py` (~100 LOC) so we can swap to `dulwich`
(pure-Python, Apache-2.0) later without touching handlers.

| Library | Verdict |
|---|---|
| **`git` CLI via subprocess** | **v1 default.** Zero deps. |
| `dulwich` (Apache-2.0) | Optional upgrade. Pure Python, no `git` on PATH. |
| `pygit2` (libgit2) | Skip. Binary dep + GPL-linking-exception. |
| `GitPython` | Skip. Maintenance mode; wraps the same subprocess calls. |
| `PyDriller` | Skip. Too heavy; builds on GitPython. |
| `gitoxide` (Rust) | Skip. Pre-1.0, no Python bindings. |

### New `pycode` views (symbol-scoped)

```python
get(kind='pycode', id='...::Registry.get', view='blame')
get(kind='pycode', id='...::Registry.get', view='log')
get(kind='pycode', id='...::Registry.get', view='churn', days=90)
get(kind='pycode', id='...::Registry.get', view='owners')
get(kind='pycode', id='...::Registry.get',
    view='diff', ref_from='v0.3.0', ref_to='HEAD')
```

- `blame` — per-line author/sha for the symbol's line range
- `log` — symbol-scoped commit history via `git log -L<start>,<end>:<file>` (rename-following for free)
- `churn` — commit count over a window; ranks sibling symbols
- `owners` — commit-weighted contributors with recency decay
- `diff` — symbol-scoped diff between two refs

Output shape matches the existing `Next:` trailer style:

```
# precis.registry.Registry.get  —  blame (src/precis/registry.py:120-128)

  L120  feat: hide unavailable kinds      4d   3a8f102  reto@…
  L121  feat: hide unavailable kinds      4d   3a8f102  reto@…
  L122  v2 walking skeleton              42d   c1e9aa4  reto@…
  L123  v2 walking skeleton              42d   c1e9aa4  reto@…
  L124  fix: NotFound options sort       12d   9b22e7d  reto@…
  …

3 commits across 3 authors. Last touched 4 days ago.

Next:
  get(...view='log')                    — full commit log for this symbol
  get(...view='churn', days=90)         — change frequency
  get(kind='git', id='precis-mcp-new', view='hot') — repo-wide hot list
```

### Sibling `git` kind (repo-scoped, language-agnostic)

Some questions aren't symbol-scoped and should work on non-Python
repos too (ansible, cluster itself):

```python
get(kind='git', id='precis-mcp-new')                          # head, branch, dirty?
get(kind='git', id='precis-mcp-new', view='log', n=20)
get(kind='git', id='precis-mcp-new', view='hot', days=30)     # hottest files
get(kind='git', id='precis-mcp-new', view='owners')           # ownership map
get(kind='git', id='precis-mcp-new',
    view='diff', ref_from='main', ref_to='HEAD')
get(kind='git', id='precis-mcp-new', view='branches')
get(kind='git', id='precis-mcp-new',
    view='blame', file='src/precis/registry.py')
search(kind='git', q='cache attribution', scope='precis-mcp-new')  # commit messages
```

Read-only. No `mode='checkout'`, no `mode='commit'` — that's
coding-agent territory and stays out of precis.

Same `pycode_index.git` helper powers both kinds. The `git` handler
is ~120 LOC on top of the shared library.

### What we steal from Aider

- **Concept**: blame + log as first-class agent affordances. Their
  `/git`, `/diff`, `/undo` chat commands inspire our `log`/`diff`
  views.
- **Not stolen here**: auto-commit / dirty-commit of LLM edits. That
  belongs to `code-repo-mcp` (the coding-agent), not `pycode` —
  `pycode` is read-only by design.

### Deferred

- **Co-change matrix** ("X changes ⇒ Y often changes") — needs a one-
  time `git log --name-only` walk with pair-count storage. Killer
  feature for review agents; phase-2 add.
- **SZZ bug-introducing-commit detection** — PyDriller has it, noisy.
- **GitHub PR / issue context** — separate `gh` kind later.
- **Cross-repo blame** (submodules, monorepo splitouts).
- **Working-tree uncommitted diff overlay** — warn via hint; don't
  pretend index state is HEAD.

## Out of scope (defer)

- Multi-language support (Rust, JS, Go) — design leaves room via
  the `kind` rename `pycode` → `code` and a backend Protocol, but
  v1 ships Python only.
- Type inference (jedi / pyright). Worth it later for accurate
  cross-file dispatch resolution.
- Test → tested-symbol mapping (would be killer for review agents,
  but needs coverage data).
- Diff-aware reindex on PR branches.
- Live LSP integration (would let pycode share an index with the
  user's editor).
- Mutation: `put(mode='edit', text=…)` is **explicitly out** —
  precis stays read-only on code; editing belongs to the editor /
  coding-agent.

## Test plan (~30 tests)

- `tests/test_pycode_outline.py` — 8 tests: classes, nested classes,
  decorators (`@dataclass`, `@property`), async functions, type-only
  imports, no-symbols file, syntax-error file (graceful degrade),
  unicode in identifiers.
- `tests/test_pycode_callgraph.py` — 8 tests: simple chain, recursion
  cycle, method-on-self, classmethod, super() call, unresolved ext,
  depth truncation, entry parsing (`module:func` vs console-script).
- `tests/test_pycode_addressing.py` — 6 tests: file id, line-range,
  symbol id, package id, ambiguous shortname, malformed id.
- `tests/test_pycode_search.py` — 4 tests: lexical hit on qualname,
  semantic hit on docstring, scope narrowing, no-match.
- `tests/test_pycode_indexer.py` — 4 tests: register / reindex / mtime
  incremental / gitignore respect.
- `tests/test_pycode_runtrace.py` — gated, 2 tests: only run when
  `PYCODE_ALLOW_EXEC=1`. Skipped in CI by default; runs locally.

## Suggested commit sequence

1. **Migration `0003_pycode.sql`** — `pycode_symbols`, `pycode_calls`,
   indexes, `pycode` corpus seed, `pycode` row in the `kinds` reference
   table.
2. **`PycodeIndexer`** in `src/precis/pycode/indexer.py` — pure logic,
   no DB. Two passes: outline + calls. Handles syntax errors. Tests.
3. **`PycodeStore` mixin** on `Store` — register, reindex, lookup
   helpers. Tests against `fresh_db`.
4. **`PycodeHandler`** in `src/precis/handlers/pycode.py` — `get`
   first (toc, outline, source); search next; put (register / reindex)
   last. Wire into `registry.py`.
5. **`callgraph` view** — tree formatter + cycle detection + depth
   truncation. Tests.
6. **`entries` view** — pyproject parser + `__main__` guard scanner.
7. **`runtrace` view** — subprocess + `sys.setprofile`; gated by env.
8. **`precis-pycode-help.md` skill** — agent-facing docs (top of file:
   "use this for any Python codebase navigation; do not paste files
   into context").
9. Self-test: register `precis-mcp-new` itself, run callgraph from
   `precis.cli:main`, paste output into the spec doc as the example.

## Done criteria

- A fresh agent can answer "explain how `precis serve` boots" without
  ever calling `read_file` or `grep_search`. Path: `view='entries'` →
  `view='callgraph' entry=…` → `outline` on the most interesting nodes
  → `source` on the one or two functions that actually need reading.
- Indexing `precis-mcp-new` (~3k LOC) takes < 2s.
- `pytest -q` adds ~30 tests, all green.
- Total LOC: ~700-1000 (indexer is the bulk).

## Open questions

1. **Repo registration UX**: `put(mode='register', text='<path>')`
   feels right, but should `id` be auto-derived from the path
   (basename) or explicit? Phase 5's ref-handler base will give us
   the patterns; align with that.
2. **Source view privacy**: should `source` redact secrets-shaped
   strings (env-var-looking literals)? Probably not — it is your own
   repo. But flag if registering a non-self repo.
3. **Cross-repo callgraph**: if two registered repos have an import
   relationship (one's pyproject lists the other), should `callgraph`
   span them? Probably yes, behind `cross_repo=True`. Defer.
4. **Async / threading**: `runtrace` under `sys.setprofile` does not
   capture cross-thread calls cleanly. Document and recommend
   `viztracer` for that case.

## Prior art summary (for the design record)

| Tool | What we borrow | What we don't |
|---|---|---|
| Aider repo-map | Token-budgeted symbol-only summary; tree-sitter mindset | PageRank ranking (we root at entry, not globally); bundling inside an editor |
| pyan / code2flow | Static call edge resolution heuristics | Graphviz output (we render trees) |
| Nuanced CodeGraph | "Code graph as MCP context layer" framing | Library-shape; standalone server |
| pyreverse | Class-hierarchy scan | UML output |
| cProfile + gprof2dot | Dynamic call graph + timings | Graphviz output; full-program-only |
| viztracer | High-fidelity tracing (optional dep) | Heavy default install |
| LSP `documentSymbol` | Per-file outline schema | Editor coupling |
| Repomix / gptree | None — those are flat dumps | Flat dumps |
