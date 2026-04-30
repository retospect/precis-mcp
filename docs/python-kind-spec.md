# `python` — Python codebase navigation kind

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
get(kind='python', id='precis-mcp')                  # repo overview
get(kind='python', id='precis-mcp', view='toc')      # package tree
get(kind='python', id='precis-mcp', view='entries')  # CLI/scripts/__main__

# Drill into a file
get(kind='python', id='precis-mcp/src/precis/registry.py')
get(kind='python', id='precis-mcp/src/precis/registry.py', view='outline')
get(kind='python', id='precis-mcp/src/precis/registry.py', view='source')
get(kind='python', id='precis-mcp/src/precis/registry.py~42-100')

# Drill into a symbol (slug = qualified dotted path)
get(kind='python', id='precis-mcp::precis.registry.Registry')
get(kind='python', id='precis-mcp::precis.registry.Registry.get')

# Composition from an entry point
get(kind='python', id='precis-mcp', view='callgraph',
    entry='precis.cli:main')
get(kind='python', id='precis-mcp', view='callgraph',
    entry='precis.cli:main', depth=3)

# Runtime overlay (opt-in; runs the code under sys.setprofile)
get(kind='python', id='precis-mcp', view='runtrace',
    entry='precis.cli:main', argv=['--help'])

# Search
search(kind='python', q='attribution footer rendering', scope='precis-mcp')
search(kind='python', q='cache TTL handling',
       scope='precis-mcp::precis.handlers')

# Edit a method (Track B — preferred, durable across edits)
put(kind='python', id='precis-mcp::precis.registry.Registry.get',
    text='''    def get(self, kind: str) -> Handler:
        """Look up a handler by kind name."""
        if kind not in self._handlers:
            raise NotFound(f"unknown kind: {kind}",
                           options=list(self._handlers))
        return self._handlers[kind]''',
    mode='replace')

# Edit a line range (Track A — when you have line numbers)
put(kind='python',
    id='precis-mcp/src/precis/registry.py~L120-128',
    text='        return self._handlers[kind]', mode='replace')

# Append a new top-level function
put(kind='python', id='precis-mcp/src/precis/registry.py',
    text='\n\ndef reset_registry() -> None:\n    """Clear handlers."""\n    global _GLOBAL\n    _GLOBAL = None\n',
    mode='append')

# Delete a deprecated method
put(kind='python',
    id='precis-mcp::precis.registry.Registry.deprecated',
    mode='delete')
```

Roots are configured at startup via `PRECIS_PYTHON_ROOTS`; there are
no `register` / `unregister` / `reindex` modes on the agent surface.
Reindex is automatic on file mtime change and exposed as a CLI
subcommand for operators.

### KindSpec

```python
KindSpec(
    kind='python', title='Python code navigator',
    supports_get=True, supports_search=True, supports_put=True,
    is_numeric=False, id_required=True,
    views=('toc', 'entries', 'outline', 'source', 'callgraph',
           'runtrace', 'imports', 'symbols',
           'blame', 'log', 'churn', 'owners', 'diff'),
    modes=('create', 'append', 'replace', 'delete'),
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

One **ref per repo** in a new `python` corpus. Repo path stored in
`refs.meta.path`. Indexing is a side-table `python_symbols`:

```sql
create table python_symbols (
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
create index on python_symbols (ref_id, file);
create index on python_symbols (ref_id, parent);
create index on python_symbols using gin (to_tsvector('english',
  coalesce(docstring, '') || ' ' || qualname));
```

A second table `python_calls` for static call edges:

```sql
create table python_calls (
  ref_id    bigint not null references refs(id) on delete cascade,
  caller    text not null,           -- qualname
  callee    text not null,           -- qualname (or "ext:<name>" for unresolved)
  file      text not null,
  line      int  not null,
  primary key (ref_id, caller, callee, file, line)
);
create index on python_calls (ref_id, caller);
create index on python_calls (ref_id, callee);
```

Both populated by the indexer on `mode='register'` and `mode='reindex'`.
Re-index is incremental by file mtime.

## Indexing pipeline

1. **Walk** repo respecting `.gitignore` (use `pathspec`).
2. **Parse** each `.py` file with `ast.parse`. No tree-sitter dep
   for v1 — stdlib `ast` is fast enough (~1ms/file) and zero-cost.
3. **Outline pass** — collect modules, classes, functions, methods,
   docstrings, signatures, line ranges → `python_symbols`.
4. **Import pass** — record `import x` and `from a.b import c` per
   module → name-resolution table (in-memory, used by step 5).
5. **Call pass** — for each `ast.Call` node, attempt static
   resolution to a qualname using:
   - local scope (function locals are ignored — too noisy)
   - module-level names + imports
   - class MRO for method calls on `self`
   - fallback `ext:<name>` for unresolved (stdlib, third-party, dynamic)
   Edges → `python_calls`.
6. **Embed** outline rows (`qualname + signature + docstring`)
   into `blocks.embedding` for semantic `search`. One block per symbol.

This is **pyan-class static analysis** done with stdlib only. We
skip duck-typed dispatch on purpose — it is the least useful 5% and
costs the most.

### When `ast` is not enough

Hooks for upgrades, behind feature flags, deferred:

- **`tree-sitter-python`** — only if multi-language support is added
  later (Rust, JS). At that point rename `python` → `code` (or add
  sibling kinds `rust`, `javascript`, `go`).
- **`jedi` / `pyright`** — for cross-file type-aware resolution.
  Slower; gates behind `requires_env=('PRECIS_PYTHON_TYPED',)`.

## Views

### `toc` — package tree

```
# precis-mcp — TOC (24 modules, 7 packages)

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
  get(kind='python', id='precis-mcp', view='entries')
  get(kind='python', id='precis-mcp::precis.registry')
  get(kind='python', id='precis-mcp', view='callgraph',
      entry='precis.cli:main')
```

### `entries` — runnable entry points

Reads `pyproject.toml` (`[project.scripts]`, `[project.entry-points]`),
finds `if __name__ == "__main__":` guards, and any `argparse.ArgumentParser()`
call sites.

```
# precis-mcp — entry points

  Console scripts:
    precis            entry: precis.cli:main          file: src/precis/cli.py:42
    precis-paper      entry: precis.cli:paper_main    file: src/precis/cli.py:118

  __main__ guards:
    src/precis/server.py:142    runs `serve()`
    src/precis/ingest.py:284    debug bulk-ingest

Next:
  get(kind='python', id='precis-mcp', view='callgraph',
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
  get(kind='python', id='precis-mcp::precis.registry.Registry')
  get(kind='python', id='precis-mcp/src/precis/registry.py~21-100',
      view='source')
```

### `outline` on a symbol — drill-down

```python
get(kind='python', id='precis-mcp::precis.registry.Registry')
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
get(kind='python', id='precis-mcp', view='callgraph',
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

#### Cross-repo resolution

When multiple repos are configured (`PRECIS_PYTHON_ROOTS=a:/...,b:/...`)
and repo `a`'s `pyproject.toml` lists `b` as a dependency (or `b` is
imported by any module in `a`), `view='callgraph'` resolves calls
into `b` instead of marking them `[ext]`. Off by default; opt in:

```python
get(kind='python', id='a', view='callgraph',
    entry='a.cli:main', depth=4, cross_repo=True)
```

Mechanism: build the symbol index per repo as today; on every
unresolved call, check the other configured repos' indexes in the
order they appear in `PRECIS_PYTHON_ROOTS` and use the first hit.
Falls back to `[ext]` only if no configured repo defines the symbol.

Resolved cross-repo calls are tagged with their repo alias in the
tree so the agent sees the boundary:

```
├── a.cli._cmd_ingest
│   └── b.lib.parser.parse                                      [b]
```

Makes "explain how `precis serve` boots" work even when boot logic
spans `precis-mcp` + `openclaw-cluster`.

### `runtrace` — dynamic overlay

Opt-in. Runs the entry point in a subprocess under `sys.setprofile`
(or `viztracer` if installed), captures the call sequence with hit
counts and elapsed time, and overlays it on the static graph.

```python
get(kind='python', id='precis-mcp', view='runtrace',
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

Sandboxing: `runtrace` is **gated by an env var** (`PRECIS_PYTHON_ALLOW_EXEC=1`)
because it executes user code. Default off. Document loudly.

**Single-threaded sync code only.** `runtrace` uses `sys.setprofile`,
which produces a clean tree for sync programs but interleaves events
from asyncio coroutines, multi-threaded code, and multiprocessing
workers into a flat soup. For those cases install `viztracer` and run
it externally. The 80% case (e.g. `precis.cli:main`) is fine.

### `imports`, `symbols` — flat helper views

- `imports` — flat dependency map (module → set of imports), useful
  for "what does this depend on" agent queries.
- `symbols` — flat list of all symbols, paginated; for completion-
  style search.

## Search

`search(kind='python', q=…)` does hybrid search over the symbol
index:
- **lexical** on `qualname || signature || docstring` (pg_trgm /
  to_tsvector)
- **semantic** on the per-symbol embedding (one block per symbol)

`scope=` accepts repo, package, or file:
```python
search(kind='python', q='cache attribution', scope='precis-mcp')
search(kind='python', q='cache attribution',
       scope='precis-mcp::precis.handlers')
search(kind='python', q='cache attribution',
       scope='precis-mcp/src/precis/handlers/_cache_base.py')
```

## Write surface

Python is **read/write in v1**. Every successful write passes through
three quality gates:

| Gate | Check | Default | Override |
|---|---|---|---|
| 1 | `ast.parse(new_content)` succeeds | mandatory | none — invariant |
| 2 | No qualname previously within the addressed region disappears in the result | on | `allow_rename=True` kwarg |
| 3 | `ruff check --fix` then `ruff format` on the result | mandatory | none — always runs (skipped only if `ruff` is missing or fails) |

Write pipeline:

1. Splice replacement text into the file buffer.
2. **Gate 1** (AST). Failure → revert, return `BadInput` with the
   parse error.
3. **Gate 2** (no-qualname-drop). Diff the symbol set inside the
   addressed region: `pre_qualnames - post_qualnames` must be
   empty. For a method-level edit the region contains exactly one
   qualname; for a class-level edit, all its methods; for a
   whole-file edit, every symbol in the module. Failure → revert,
   return `BadInput` listing the dropped qualnames.
4. **Gate 3** (`ruff check --fix` then `ruff format`). Output is
   the canonical content. Both run unconditionally; ruff applies
   the safe autofixes it knows (unused imports, redundant `not in`,
   `is None` vs `== None`, sorted `__all__`, etc.) and then
   normalises layout. Unfixable lint findings are *not* a hard
   gate — they pass through to the response as a note so the agent
   can decide. The post-fix + post-format buffer is what hits disk.
5. Atomic write to disk (see below).
6. Force re-ingest (mtime + sha256 changed → re-parse → update
   symbol table). Held under a write lock so a concurrent `get`
   waits.
7. Return response computed from the new symbol table — line ranges
   reflect the **post-fix-and-format** state. If ruff changed the
   buffer (either step), the response includes a one-line summary
   of what changed.

### Gate 2 — the no-qualname-drop check

Naming aside, gate 2 protects against two related bugs:

- **Accidental rename.** Replace `Class.method_a` with a body whose
  `def` line names `method_b`. The addressed qualname vanished;
  callers break.
- **Accidental drop.** Replace whole `Class` with a rewritten body
  that focuses on one method and forgets the others existed. The
  addressed `Class` still resolves; nested members vanish silently;
  callers break.

Both are caught by the same set-diff: any qualname that was inside
the addressed region before the edit must still be inside it (or at
least somewhere in the file) after. Cheap because the symbol table
is already in memory.

The `allow_rename=True` kwarg overrides the check entirely — use it
when the rename or drop is intentional. The flag name reads
slightly off for the drop case, but one knob is better than two and
the meaning ("yes, I know I'm changing the symbol set") is clear.

### Atomic-immediate write semantics

When `put` returns successfully, the new bytes are on disk. There
is no in-memory buffer, no deferred batch.

1. Write to `<file>.tmp.<pid>.<rand>` in the same directory as the
   target (required for `os.replace` to be atomic on the same
   filesystem).
2. `fsync(tmp)` — force buffered writes to physical storage before
   the rename.
3. `os.replace(tmp, target)` — atomic on POSIX; single inode swap.
4. `fsync(directory)` — ensures the rename itself is durable across
   power loss.

Concurrent readers see exactly the old or exactly the new content,
never a partial mix. On crash mid-write the tmpfile may exist (a
startup pass cleans up `*.tmp.*` orphans); the target is intact.

Between the disk write and the re-ingest completing (a few ms) the
symbol table is briefly stale. A write lock around steps 6–7 makes
concurrent `get` calls wait, so end-to-end every put returns only
after disk + index are caught up.

### Modes

| Mode | Address shape | Behavior |
|---|---|---|
| `create` | `repo/path/to/new.py` (no selector) | new file; refuses overwrite |
| `replace` | file (no selector), file + selector, or qualname | swap region |
| `append` | file (no selector) | append at file end |
| `delete` | file + selector or qualname | remove region |

Selectors for `replace` / `delete`:

- `~L<a>-<b>` — Track A line range
- `~Symbol` or `~Class.method` — local Track B symbol
- `::pkg.mod.Symbol.method` — qualname shortcut (alternative to
  file-path + selector)

### Indentation — strict

The replacement text replaces lines `[start, end]` verbatim. The
agent supplies the indentation. The handler does **not** dedent-and-
re-indent — that hides bugs in the replacement text. Round-trip is
`view='source'` → modify → write; indentation is preserved by
construction.

### Format-on-write rationale

`ruff format` runs every successful write because:

1. Project culture demands `ruff format --check` cleanness on commit.
   Skipping format-on-write means every commit needs a manual `ruff
   format` pass before push.
2. The agent doesn't have to match exact whitespace, quote style,
   trailing-comma policy. Submit semantically correct Python; the
   formatter handles surface details.
3. The post-format line range in the response is the durable answer.
   If `ruff` shifts lines, the response reflects that.

**Invocation.** Two subprocess calls per write, both via stdin:

```python
fixed = subprocess.run(
    ['ruff', 'check', '--fix', '--exit-zero',
     '--stdin-filename', str(path)],
    input=buffer, capture_output=True, text=True, check=True,
).stdout
formatted = subprocess.run(
    ['ruff', 'format', '--stdin-filename', str(path)],
    input=fixed, capture_output=True, text=True, check=True,
).stdout
```

`--exit-zero` on the fix step means "don't fail if there are
unfixable findings"; we capture them on stderr and surface in the
response, but the write proceeds. The format step is an unconditional
layout pass.

There is no Python API for ruff (it is a Rust binary; the PyPI
`ruff` package just ships the CLI). The third-party `ruff-api` PyO3
wrapper is unmaintained.

**Config discovery.** `ruff check` and `ruff format` natively walk
up from `--stdin-filename` looking for `pyproject.toml` /
`ruff.toml`. We let them — writes follow whatever style the project
pinned (line length, quote style, trailing-comma policy, enabled
rules). Matches what the user would get typing `ruff check --fix
file.py && ruff format file.py` interactively, so commit checks see
no surprises. For repos without ruff config, ruff defaults apply.

**Cost.** ~20ms subprocess startup (two calls) + ~50ms total fix +
format for a 2k-line file. Acceptable for interactive writes; no
batch-defer switch. If `ruff` is missing or fails, the write
proceeds with the unfixed/unformatted buffer and a warning header.
Ruff never blocks a write.

**Reporting changes.** When ruff modifies the buffer (either step),
the response includes a summary so the agent learns from the
correction rather than being silently fixed:

```
  ruff:  3 changes
    - removed 1 unused import (`json`)
    - sorted `__all__`
    - 4 whitespace adjustments (format)
```

The summary distinguishes **fix** changes (semantic; ruff removed an
unused import) from **format** changes (layout-only; whitespace,
quote style, trailing commas). If ruff didn't change anything, the
line reads `ruff: no changes`.

### What writes do not do

- **Auto-import management.** Adding a method that needs a new
  import requires a separate put on the file's import region. A
  future `mode='import'` helper may add this.
- **Cross-file rename.** A rename is a delete + create + reference
  update. v1 does not do reference updates; chain put calls or use
  the editor's rename refactoring.
- **Type checking.** `mypy` is not run.
- **Test running.** Out of scope.
- **Auto-commit.** No `git commit` after a write — the working tree
  is left dirty for the user / coding agent to review.

### Response shape

```
# precis.registry.Registry.get  —  replace (src/precis/registry.py:120-126)

  ast.parse:           ok
  qualname preserved:  ok
  ruff:                3 changes
    - removed 1 unused import (`json`)
    - reformatted call site (line 124)
    - 1 whitespace adjustment

Replaced lines 120–128 → 120–126 (-2 lines).

Next:
  get(kind='python', id='precis-mcp::precis.registry.Registry.get')
  get(kind='python', id='precis-mcp::precis.registry.Registry.get', view='blame')
```

Same two-track header as read responses, plus a gate report (with
ruff changes when applicable) and a diff summary.

## Git integration

The symbol index already knows `(file, start_line, end_line)` for every
qualname, so mapping any symbol to its git history is nearly free. Git
becomes first-class in `python`, plus a small sibling `git` kind for
repo-scoped questions that don't need symbols.

### Library choice

Shell out to `git` via `subprocess` for v1. Zero deps, every cluster
node has git, same pattern the existing `code-repo-mcp` uses. Wrap
behind `python_index/git.py` (~100 LOC) so we can swap to `dulwich`
(pure-Python, Apache-2.0) later without touching handlers.

| Library | Verdict |
|---|---|
| **`git` CLI via subprocess** | **v1 default.** Zero deps. |
| `dulwich` (Apache-2.0) | Optional upgrade. Pure Python, no `git` on PATH. |
| `pygit2` (libgit2) | Skip. Binary dep + GPL-linking-exception. |
| `GitPython` | Skip. Maintenance mode; wraps the same subprocess calls. |
| `PyDriller` | Skip. Too heavy; builds on GitPython. |
| `gitoxide` (Rust) | Skip. Pre-1.0, no Python bindings. |

### New `python` views (symbol-scoped)

```python
get(kind='python', id='...::Registry.get', view='blame')
get(kind='python', id='...::Registry.get', view='log')
get(kind='python', id='...::Registry.get', view='churn', days=90)
get(kind='python', id='...::Registry.get', view='owners')
get(kind='python', id='...::Registry.get',
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
  get(kind='git', id='precis-mcp', view='hot') — repo-wide hot list
```

### Sibling `git` kind (repo-scoped, language-agnostic)

Some questions aren't symbol-scoped and should work on non-Python
repos too (ansible, cluster itself):

```python
get(kind='git', id='precis-mcp')                          # head, branch, dirty?
get(kind='git', id='precis-mcp', view='log', n=20)
get(kind='git', id='precis-mcp', view='hot', days=30)     # hottest files
get(kind='git', id='precis-mcp', view='owners')           # ownership map
get(kind='git', id='precis-mcp',
    view='diff', ref_from='main', ref_to='HEAD')
get(kind='git', id='precis-mcp', view='branches')
get(kind='git', id='precis-mcp',
    view='blame', file='src/precis/registry.py')
search(kind='git', q='cache attribution', scope='precis-mcp')  # commit messages
```

Read-only. No `mode='checkout'`, no `mode='commit'` — that's
coding-agent territory and stays out of precis.

Same `python_index.git` helper powers both kinds. The `git` handler
is ~120 LOC on top of the shared library.

### What we steal from Aider

- **Concept**: blame + log as first-class agent affordances. Their
  `/git`, `/diff`, `/undo` chat commands inspire our `log`/`diff`
  views.
- **Not stolen here**: auto-commit / dirty-commit of LLM edits. That
  belongs to `code-repo-mcp` (the coding-agent), not `python`.
  Writes leave the working tree dirty; commit policy is the user's.

### Deferred

- **Co-change matrix** ("X changes ⇒ Y often changes") — needs a one-
  time `git log --name-only` walk with pair-count storage. Killer
  feature for review agents; phase-2 add.
- **SZZ bug-introducing-commit detection** — PyDriller has it, noisy.
- **GitHub PR / issue context** — separate `gh` kind later.
- **Cross-repo blame** (submodules, monorepo splitouts).
- **Working-tree uncommitted diff overlay** — warn via hint; don't
  pretend index state is HEAD.
- **`runtrace_engine='viztracer'`.** Frame-eval / sample-based
  tracing for async + multi-threaded programs. Optional dep;
  expose when a real use case lands.

## Out of scope (defer)

- Multi-language support (Rust, JS, Go) — design leaves room via
  the `kind` rename `python` → `code` and a backend Protocol, but
  v1 ships Python only.
- Type inference (jedi / pyright). Worth it later for accurate
  cross-file dispatch resolution.
- Test → tested-symbol mapping (would be killer for review agents,
  but needs coverage data).
- Diff-aware reindex on PR branches.
- Live LSP integration (would let python share an index with the
  user's editor).
- Auto-import management on writes (a `mode='import'` helper that
  inserts at the right place). v1 punts to the agent.
- Cross-file rename refactoring. v1 supports per-symbol delete /
  create / replace; reference updates are not automatic.

## Test plan (~46 tests)

- `tests/test_python_outline.py` — 8 tests: classes, nested classes,
  decorators (`@dataclass`, `@property`), async functions, type-only
  imports, no-symbols file, syntax-error file (graceful degrade),
  unicode in identifiers.
- `tests/test_python_callgraph.py` — 9 tests: simple chain, recursion
  cycle, method-on-self, classmethod, super() call, unresolved ext,
  depth truncation, entry parsing (`module:func` vs console-script),
  cross-repo resolution (`cross_repo=True` resolves into a sibling
  configured repo; tagged in tree).
- `tests/test_python_addressing.py` — 6 tests: file id, line-range,
  symbol id, package id, ambiguous shortname, malformed id.
- `tests/test_python_search.py` — 4 tests: lexical hit on qualname,
  semantic hit on docstring, scope narrowing, no-match.
- `tests/test_python_indexer.py` — 4 tests: lazy reindex on mtime
  change / gitignore respect / sha256 unchanged short-circuit / hash
  drift triggers re-parse.
- `tests/test_python_write.py` — 15 tests: replace by qualname /
  replace by line range / replace whole file / append / create /
  refuse-create-overwrite / delete by qualname / AST-syntax-error
  reverts / no-qualname-drop reverts on accidental method rename /
  no-qualname-drop reverts on whole-class replace that drops nested
  methods / `allow_rename=True` permits rename and drop / `ruff
  check --fix` removes unused imports + reports the change in
  response / `ruff format` applied + line ranges in response
  reflect post-fix-and-format state / `ruff` missing falls through
  with warning header / atomic-write durability (concurrent reader
  sees old or new, never partial).
- `tests/test_python_runtrace.py` — gated, 2 tests: only run when
  `PRECIS_PYTHON_ALLOW_EXEC=1`. Skipped in CI by default; runs locally.

## Suggested commit sequence

1. **Migration `0005_python.sql`** — `python_symbols`, `python_calls`,
   indexes, and a `python` row in the `kinds` reference table.
   (Migration 0004 already registered the other file kinds; `python`
   is added here rather than in 0004 so it lands in the same commit
   as the indexer that gives it meaning.)
2. **`PythonIndexer`** in `src/precis/python/indexer.py` — pure logic,
   no DB. Two passes: outline + calls. Handles syntax errors. Tests.
3. **`PythonStore` mixin** on `Store` — lookup helpers (qualname →
   file+line, file → outline). Tests against `fresh_db`.
4. **`PythonHandler.get` + `.search`** in `src/precis/handlers/python.py`.
   Read surface only — toc, outline, source, callgraph stub. Wire
   into `registry.py`.
5. **`PythonHandler.put`** — the four modes + four validation gates.
   Reuses splice / atomic-write helpers from `_FileHandlerBase`
   (extracted when phase 6b shipped). New python-specific: AST gate,
   qualname preservation, `ruff format` subprocess, optional lint.
6. **`callgraph` view** — tree formatter + cycle detection + depth
   truncation. Tests.
7. **`entries` view** — pyproject parser + `__main__` guard scanner.
8. **`runtrace` view** — subprocess + `sys.setprofile`; gated by env.
9. **`precis-python-help.md` skill** — agent-facing docs (top of file:
   "use this for any Python codebase navigation; do not paste files
   into context"). Already drafted; refresh against the implementation.
10. Self-test: index `precis-mcp` itself, run callgraph from
    `precis.cli:main`, paste output into the spec doc as the example.

## Done criteria

- A fresh agent can answer "explain how `precis serve` boots" without
  ever calling `read_file` or `grep_search`. Path: `view='entries'` →
  `view='callgraph' entry=…` → `outline` on the most interesting nodes
  → `source` on the one or two functions that actually need reading.
- Indexing `precis-mcp` (~3k LOC) takes < 2s.
- `pytest -q` adds ~46 tests, all green.
- A fresh agent can edit a method by qualname and the resulting file
  passes `ast.parse`, `ruff check`, and `ruff format --check`
  without manual fixup.
- Total LOC: ~900-1200 (indexer + read views ~700; write surface
  ~200-300 on top, mostly shared with `_FileHandlerBase`).

## Open questions

*(none currently — all v1 design decisions resolved.)*

Closed questions, for the record:

- **Source view privacy** — dropped. Secrets don't belong in `.py`
  files; redacting at view time is theatre and pattern-matching
  has too many false positives. `source` shows source.
- **Cross-repo callgraph** — in v1 behind `cross_repo=True` (see
  `callgraph` view § Cross-repo resolution).
- **`ruff format` config discovery** — resolved: ruff CLI walks up
  natively for `pyproject.toml` / `ruff.toml`.
- **Format-on-write performance** — resolved: ~60ms is fine; no
  switch.
- **Class-replace preservation** — resolved: gate 2 is a single
  no-qualname-drop set diff, covering both rename and drop.
- **Async / threading runtrace fidelity** — resolved: `runtrace`
  is documented as single-threaded-sync only; recommend
  `viztracer` for the rest.

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
