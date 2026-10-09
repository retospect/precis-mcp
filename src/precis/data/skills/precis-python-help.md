---
id: precis-python-help
title: precis — navigate and edit Python codebases
summary: Python code navigation and edits — qualname or file/line addressing, ruff-gated writes
answers:
  - how do I point at a Python symbol or a line range?
  - I'm new to this repo — where do I start orienting?
  - how do I find every caller of a function?
  - how do I trace the call path from a console script entry point to a function?
  - how do I replace a function body by its qualname?
applies-to: get/search/put/edit/delete (kind='python')
tags: orientation, addressing
kinds: python
status: active
---

# precis-python-help — navigate and edit Python codebases

Python codebases addressable as a graph: by file + lines, or by
qualname. Writes are gated through `ast.parse` + `ruff check --fix`
+ `ruff format` before atomic rename.

Don't paste files into context to orient. Use the map first, the
source last.

## What does a python id look like?
## Python address grammar — file/lines vs qualname
## How do I point at a Python symbol or line range?

Two distinct address tracks:

```text
Track A — file + lines:   <alias>/<rel/path>.py~L<a>-L<b>
Track B — symbol:         <alias>::<dotted.qualname>
                          <alias>/<rel/path>.py~<Class.method>
```

| Track | Form | Use when |
|---|---|---|
| A — coordinates | `precis/src/precis/cli.py~L42-58` or `~L120` | from a stack trace, grep, IDE |
| B — symbol | `precis::precis.cli.main` or `~Hub.register_ability` | durable; survives edits above |

The alias comes from `PRECIS_PYTHON_ROOTS` (e.g.
`PRECIS_PYTHON_ROOTS=precis:/path/to/precis,cluster:/path/to/cluster`
gives aliases `precis` and `cluster`). The `::` separator is python-
specific and goes straight to a dotted qualname; `/` introduces a
file path and `~` introduces a selector inside it.

Worktree aliases: when the server has `PRECIS_PYTHON_WORKTREES`, every git
worktree of the main checkout is a root `wt-<tree name>` (e.g.
`wt-validate-precis-python::pkg.mod.fn`), discovered live (about 5 s lag), no
restart. They are read-only (write verbs refuse; use your own Edit tool), built
lazily on first query, and stay indexed until unused for 24 h
(`PRECIS_PYTHON_WORKTREE_IDLE_HOURS`) or the tree is removed. They are NOT in the default cross-root
search; name one with `scope='wt-x'` (or `wt-x::pkg.mod`). Semantic vectors
are not warmed for them (cached vectors from main still hit). If the alias does
not resolve, fall back to Grep.

## Which checkout did this result use?

Every successful Python read/search carries ONE summary line per consulted
root, right after the headline and before `Python content:`:

```
checkout: main@/path/to/main · corpus 480d4806 (2677 files, stat-checked, 0 parse errors) · git 1a726c44 clean (observed) — details: get(kind='python', id='main', view='provenance')
```

`corpus` is the first 8 hex of the indexed-bytes fingerprint; the Git part is
a separate observation and never certifies that the indexed bytes equal HEAD.
Reads of exactly one file add ` file <path> <8-hex digest>`. Non-default states
are upper-cased in the line: `DIRTY`, `dirty=UNKNOWN`, `git UNAVAILABLE`,
`PARTIAL` (Git), `PARTIAL WALK`, `PARSE ERRORS n`, `root UNAVAILABLE`,
`freshness UNKNOWN`; `(expected_root ok)` appears when you asserted one.

Drill down with `view='provenance'` on an alias, file or symbol id: it renders
the full block (alias/root, corpus framing and full SHA256, freshness window,
reparsed/reused counts, index limitations, parse-error count, the exact
`Indexed file:` raw-byte hash and `indexed_at` for that file/symbol, and the
full Git state with branch, dirty scope and observation window). The corpus
hash frames sorted paths and exact indexed raw-byte SHA256 values; it excludes
non-Python files, symlinks and hidden/skip directories, and is not a Git tree
hash. File/symbol source comes from the indexed snapshot; reused modules are
stat-checked, content not revalidated (shown in the drill-down). Walks and Git
observations are non-atomic. Entry discovery labels separately read project
metadata. Runtrace provenance does not attest executed bytes.

`get(kind='python')` lists configured aliases and availability without indexing
every root. Assert a known alias's expected root before indexing:

```python
get(kind='python', id='precis::pkg.mod.func', args={'expected_root': '/absolute/task/root'})
search(kind='python', q='cache', scope='precis', args={'expected_root': '/absolute/task/root'})
```

`expected_root` requires an explicit alias id/scope and grants no access or
registration. On mismatch, select an existing correct alias or have the operator
configure a separately authorized local server for that directory. Roots are
registered at server construction; there is no request-time arbitrary-path fallback. Do not bind
production to an unreviewed mutable task checkout or edit through another root.

```python
get(kind="python", id="precis")  # repo overview
get(kind="python", id="precis/src/precis/cli.py")  # file outline
get(kind="python", id="precis/src/precis/cli.py~L120")  # one line (Track A)
get(kind="python", id="precis/src/precis/cli.py~L96-130")  # line range
get(kind="python", id="precis/src/precis/service.py~Hub")  # local symbol (Track B)
get(kind="python", id="precis/src/precis/service.py~Hub.register_ability")
get(
    kind="python", id="precis::precis.service.Hub.register_ability"
)  # qualname shortcut
```

Ambiguous qualnames return `BadInput` with `options=` listing every
matching qualname.

Nested functions are symbols: `outer.inner`, method-nested
`Class.meth.helper`. A repeated nested name under one parent gets
`name#2`, `name#3` in source order. File outlines show top-level defs
only; reach nested ones by qualname.

## Find the right place to start
## I'm new to this repo — where do I begin?
## Orient in an unfamiliar Python codebase

```python
search(kind="python", q="cache attribution", scope="precis")
search(kind="python", q="where do we handle stale data", scope="precis", page=2)
```

Default search is hybrid: lexical score fused with embedding similarity
of each symbol's qualname + signature + first docstring paragraph, so
paraphrases ("stale data" -> cache invalidation) find symbols that share
no words with the query. A query that is a substring of a qualname still
ranks that symbol first. Hits are canonical addresses you can paste as
`id=`; `sim=` shows the semantic similarity. `mode='lexical'` /
`mode='semantic'` use one half only.

The symbol index is built in the background after server start. Until it
is ready (or with no embedder configured) the answer is lexical-only and
a one-line `(...)` note under the headline says so; retry in a minute for
semantic hits. Status: `python_vector_warmup` in `precis-status`.

## Read a file or a symbol
## Open Python source

```python
get(kind="python", id="<alias>")  # repo overview
get(kind="python", id="<alias>", view="toc")  # module/package tree
get(kind="python", id="<alias>", view="entries")  # console scripts + __main__
get(kind="python", id="<alias>/<path>.py")  # file outline (default)
get(
    kind="python", id="<alias>/<path>.py", view="outline"
)  # outline w/ type annotations
get(kind="python", id="<alias>/<path>.py", view="source")  # raw source
get(
    kind="python", id="<alias>::<qualname>"
)  # signature + docstring + callers + callees
get(kind="python", id="<alias>::<qualname>", view="source")  # body verbatim
```

Every symbol view shows parent, callers, callees, raises — one call,
many edges traversable.

## Views

| View | What it shows |
|---|---|
| (default for repo) | package tree |
| (default for file) | imports + class/function tree |
| (default for symbol) | signature + docstring + decorators + raises + callers + callees |
| `toc` | repo-wide module/package tree |
| `outline` | per-file outline with type annotations |
| `source` | raw source for the resolved region |
| `entries` | console scripts + `__main__` guards |
| `callgraph` | entry-rooted static call tree (needs `args={'entry': ...}`); a module entry lists its top-level callables |
| `provenance` | checkout/corpus/Git detail for an alias, file or symbol id |
| `runtrace` | dynamic trace; gated by `PRECIS_PYTHON_ALLOW_EXEC=1` |
| `callers` | call sites that reference a symbol (`<alias>::<qualname>`) |
| `importers` | modules that import a module (`<alias>::<module>`) |
| `imports` | what a module imports, in-repo vs external |

## Map a stack trace to a symbol
## I have a line number — what symbol is it in?

```python
get(kind="python", id="precis/src/precis/service.py~L444")
# Response resolves L444 → boot (lines 444-612). Then:
get(kind="python", id="precis::precis.service.boot")
```

A Track-A read ends with `range: L444-444 sha=1a2b3c4d` (see "Edit by
line range") and an `Enclosing symbol:` note naming the innermost
symbol(s) and a `Next:` to open it. `view='source'` on a symbol prints the
same `range:` line for its span.

## Trace a boot path
## How does the `precis` entry point reach this function?
## Walk the call graph from a console script

```python
get(kind="python", id="precis", view="entries")
# → entry: precis.cli:main  (setuptools shorthand)

get(
    kind="python",
    id="precis",
    view="callgraph",
    args={"entry": "precis.cli.main:main", "depth": 3},
)
```

`callgraph` resolves on the fully-qualified form
(`precis.cli.main:main`), not the setuptools shorthand
(`precis.cli:main`). If the shorthand returns a stub, expand it.

`args=` keys for `callgraph` / `runtrace`:

| Key | View | Default |
|---|---|---|
| `entry` | both | required (`'module:func'` or `'module.func'`) |
| `depth` | `callgraph` | 3 (1–10) |
| `cross_repo` | both | False |
| `argv` | `runtrace` | `[]` |
| `env` | `runtrace` | inherits |
| `timeout` | `runtrace` | 10s (1–60) |
| `max_events` | `runtrace` | 2000 (1–1_000_000) |
| `expand_stdlib` | `runtrace` | False — folds stdlib subtrees by default |

Don't put reserved kwargs (`kind` / `id` / `view` / `q`) inside
`args=` — the boundary rejects with `BadInput`.

## Find every caller of a function
## Who calls this symbol?

```python
get(kind="python", id="precis::precis.service.Hub.register_ability")
# Default symbol view includes Called by: and Calls: sections.

get(kind="python", id="precis::precis.service.Hub.register_ability", view="callers")
# One row per call site: r::<caller-qualname>  file:line.
```

`callers` lists resolved edges first, then unresolved call sites with
the same method name (`handler.search(...)` where the receiver's type
isn't tracked) as a labelled lead; bare same-name calls that resolve
nowhere are listed as leads too. Calls through function-local imports,
inside lambdas (credited to the enclosing function) and inside nested
functions resolve. No type inference, no MRO walk; `getattr` dispatch
is invisible.

## Who imports this module, and what does it import?

```python
get(kind="python", id="precis::precis.handlers.python", view="importers")
get(kind="python", id="precis::precis.handlers.python", view="imports")
```

`importers` rows are `r::<module>  file  [bound names]`. Imports inside
function bodies (nested functions included) are listed as
`name (in <func>)`. Not indexed: imports in classes defined inside
functions, `exec`, `importlib.import_module`.

## Find symbols by decorator, async, or regex

```python
search(kind="python", mode="pattern", q="async @router\\.get")
```

`mode='pattern'` ANDs whitespace-separated terms: `async`, `@regex`
(matches a decorator), or a regex on qualname / signature. Same
`scope=` and `page_size=` as the lexical search.

## Edit a symbol by qualname
## Replace a function body — preferred edit form

```python
edit(
    kind="python",
    id="precis::precis.service.Hub.handler_for",
    text='''    def handler_for(self, kind: str) -> Any | None:
        """Return the handler registered for ``kind``, or None."""
        return self.handlers.get(kind)''',
    mode="replace",
)
```

```text
replaced precis.service.Hub.handler_for (lines 204-206 → 204-206)
ast.parse:           ok
qualname preserved:  ok
ruff:                no changes
```

Qualnames survive file moves and re-orderings — prefer this form.
The response gives the **post-format** line range; use those in
follow-ups.

## Edit by line range
## Replace lines when I have line numbers

Prefer qualname edits (above); they are position-independent. Use line
ranges only when you have coordinates, and guard them: read, note the
`range: L<a>-<b> sha=<8hex>` line, pass it back as `base_sha=`.

```python
get(kind="python", id="precis/src/precis/service.py~L204-206")
# ... range: L204-206 sha=1a2b3c4d

edit(
    kind="python",
    id="precis/src/precis/service.py~L204-206",
    text="        return self.handlers.get(kind)",
    mode="replace",
    base_sha="1a2b3c4d",
)
```

```text
range: L204-204 sha=9f8e7d6c
relocated: L204-L206 -> L207-L209 (content moved by 3 lines down)
replaced lines 204-206 -> 204
ast.parse:       ok
```

`sha` = first 8 hex of sha256 over the exact bytes of those lines
(newlines included, `\n`-normalised). Works with replace, find-replace
and insert (delete = `replace` with `text=''`), and `dry_run`.
- Lines still hash to `base_sha`: edit applies as addressed.
- They moved (lines inserted/removed above): if exactly one same-length
  run hashes to it, the edit lands there and says `relocated: ...`.
- Changed, or several runs match: refused, file untouched; the error
  gives the current sha, the matches, and a `Next:` to re-read (or edit
  the enclosing symbol by qualname).
- No `base_sha`: applies as addressed, plus a `hint:` line. The edit
  response's `range:` line is the sha for chaining the next guarded edit.

Line numbers are 1-indexed, inclusive both ends (vi/sed/GitHub
permalink convention). `L120-128` is 9 lines; `L120` is one.

## Surgical edits inside a function
## Rename one call site, fix one literal

```python
# Anchored rename within one symbol.
edit(
    kind="python",
    id="precis::precis.service.boot",
    mode="find-replace",
    find="deprecated_call(",
    text="new_call(",
    match="all",
)

# Disambiguate via surrounding context.
edit(
    kind="python",
    id="precis/src/precis/service.py",
    mode="find-replace",
    find="name",
    before="len(",
    after=")",
    text="full_name",
)

# Insert adjacent to an anchor.
edit(
    kind="python",
    id="precis/src/precis/service.py",
    mode="insert",
    find="    return x + 1\n",
    where="after",
    text="\n\ndef twice(x: int) -> int:\n    return x * 2\n",
)

# Span delete — text='' is the delete idiom.
edit(
    kind="python",
    id="precis::precis.service.Hub.handler_for",
    mode="find-replace",
    find="    # TODO: revisit\n",
    text="",
)
```

The selector decides the search region: `~L20-L40` scopes the
range; `~func` or `::pkg.mod.func` scopes one symbol; bare file id
scopes the whole file. `match='unique'` is the default — multiple
matches return every candidate's line number with a hint to add an
anchor. Use `match='all'` / `match='first'` to override.

Pass `dry_run=True` to preview gates without writing; `dry_run='full'`
emits the post-edit region. Full grammar in `precis-edit-help`.

## Create a new file
## Append a new top-level function

```python
# Create.
put(
    kind="python",
    id="precis/src/precis/handlers/audit.py",
    text='''"""Audit handler."""
from precis.protocol import Handler


class AuditHandler(Handler):
    pass
''',
    mode="create",
)

# Append a top-level function.
edit(
    kind="python",
    id="precis/src/precis/service.py",
    text='''

def registered_kinds(hub: Hub) -> list[str]:
    """Snapshot of every kind currently registered on ``hub``."""
    return sorted(hub.kinds)
''',
    mode="append",
)
```

`mode='create'` refuses to overwrite; use `mode='replace'` on a
bare file id to swap a whole file.

## Delete a method
## Drop a symbol from a class

```python
delete(kind="python", id="precis::precis.service.Hub.deprecated")
```

```text
deleted precis.service.Hub.deprecated (lines 145-152)
ast.parse:           ok
qualname removed:    ok
ruff:                2 changes
  - removed 1 unused import (`json`)
  - 1 whitespace adjustment (format)
```

Whole-file delete is rejected — that's `rm` / `git rm` territory.
precis manages content; delete the file with your OS tool and the
next `get` soft-deletes the ref.

## Write gates and what can go wrong
## What does the AST + ruff pipeline check?

Every write runs `ast.parse` → `ruff check --fix` → `ruff format`
before atomic rename. If ruff modifies, the response itemises the
changes (which import was unused, which `__all__` was re-sorted) so
you learn style mismatches one write at a time.

| Problem | Response |
|---|---|
| Replacement doesn't parse | `BadInput("ast.parse failed: <error>")` — file untouched |
| Edit drops a qualname from the addressed region | `BadInput("qualname(s) dropped: Class.method_b, …")` — pass `allow_rename=True` to override |
| Renaming a `def` line via `mode='edit'` | Rejected unless `allow_rename=True` |
| Indentation mismatch | Replacement spliced verbatim; supply correct indent (use `view='source'` first) |
| Line range out of bounds | `BadInput("line range L<a>-<b> outside file (1–<n>)")` |
| Empty range (end < start) | `BadInput("empty range: end < start")` |

The drop check is a set-diff: every qualname inside the addressed
region before the edit must still exist after. Catches accidental
renames and accidental deletions when copying a class body.

## Workflow: read → modify → write

```python
get(kind='python', id='precis::precis.registry.Registry.get', view='source')
# returns indented body
# modify locally
edit(kind='python', id='precis::precis.registry.Registry.get',
    text=<modified source>, mode='replace')
```

Step 1 returns native indentation; step 3 takes it back verbatim —
indentation is preserved by construction.

You handle yourself: imports (add via a separate `edit` at file
top), cross-file rename (delete + create + reference updates), and
commits (working tree is left dirty).

## See also

- [[precis-files-help]] — shared file address grammar
- [[precis-edit-help]] — anchored find-replace + insert grammar
- [[precis-markdown-help]] — .md block grammar
- [[precis-overview]] — verbs and kinds
