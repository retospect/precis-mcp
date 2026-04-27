---
id: precis-python-help
title: precis — navigate Python codebases
status: spec (unbuilt)
tier: 1
floor: any
applies-to: get/search/put (kind='python'); writes are AST-validated, ruff-fixed, and ruff-formatted
last-updated: 2026-04-27
---

# precis-python-help — Python codebase navigation

For shared addressing (two tracks, multi-root, response shape) read
`precis-files-help` first. This skill covers what's special about
**code**: headers carry **graph metadata**.

> Use `python` for any Python codebase navigation task. **Do not**
> paste files into context to orient yourself; that wastes tokens
> on imports, boilerplate, and whitespace. `python` gives you the
> map first, the source last.

## What makes code different

A Python **header** (a class, function, or method) sits in a graph:

```
Registry
  ├── parent          : module precis.registry
  ├── methods         : __init__, get, kinds, __contains__, __len__
  ├── inherits        : object
  ├── called by       : precis.runtime.build_runtime, …
  └── calls           : precis.errors.NotFound.__init__
```

Every header view shows these edges so you can navigate as a graph,
not as a file tree. **One call from the agent's perspective; many
edges traversable in one response.**

## Three address forms

Python accepts three id shapes:

```python
# 1. File path — like every other file kind. Same Track A/B rules.
get(kind='python', id='precis/src/precis/registry.py')
get(kind='python', id='precis/src/precis/registry.py~Registry')
get(kind='python', id='precis/src/precis/registry.py~Registry.get')
get(kind='python', id='precis/src/precis/registry.py~L42-100')

# 2. Qualname shortcut — when you know the dotted name, skip the path.
get(kind='python', id='precis::precis.registry.Registry.get')

# 3. Repo overview.
get(kind='python', id='precis')
get(kind='python', id='precis', view='toc')
get(kind='python', id='precis', view='entries')
```

The `::` shortcut is python-specific. The handler's symbol index
maps qualname → file + line range, so you don't have to know the
file. If the qualname is ambiguous, you get `BadInput` with
`options=` listing all matching qualnames.

## Two-track addressing — same as everywhere else

| Track | Form | When |
|---|---|---|
| **A — coordinates** | `~L<a>-<b>` | from a stack trace, grep, IDE |
| **B — headers** | `~Registry.get` or `::precis.registry.Registry.get` | durable, graph-rich |

```python
# Track A: I have a line from a traceback.
get(kind='python', id='precis/src/precis/cli.py~L142')
# → Resolved L142 to function _cmd_serve (lines 138-150).

# Track B: I have a qualname.
get(kind='python', id='precis::precis.cli._cmd_serve')
```

Track B in python is **not just a slug** — every header response
carries its graph context (parent, callers, callees, raises).

## Views

| View | What it shows |
|---|---|
| (default for repo) | package tree (modules, packages) |
| (default for file) | imports + class/function tree |
| (default for symbol) | signature + docstring + decorators + raises + callers + callees |
| `outline` | richer per-file outline with type annotations |
| `source` | raw source for the resolved region |
| `callgraph` | entry-rooted call tree (requires `entry='module:func'`) |
| `runtrace` | dynamic trace (gated by `PRECIS_PYTHON_ALLOW_EXEC=1`) |
| `imports` | flat dependency map |
| `symbols` | flat paginated symbol list |
| `blame` / `log` / `churn` / `owners` / `diff` | git overlays |

## Recipes

### Find the right place to start

```python
# 1. Semantic search across the symbol index — qualname + signature + docstring.
search(kind='python', q='cache attribution', scope='precis')

# 2. The hits come back as canonical addresses you can drill into.
get(kind='python', id='precis::precis.handlers._cache_base.CacheBackedHandler')
```

This is the single most token-efficient way to orient in an unfamiliar
repo. Beats `grep` because it understands intent (`q='where do we
handle stale data'` works); beats reading files cold because it returns
exactly the symbols, not their bodies.

### Map a stack trace to symbols

```python
# `Traceback … File "src/precis/cli.py", line 142 …`
get(kind='python', id='precis/src/precis/cli.py~L142')

# Response gives you both forms:
#   precis/src/precis/cli.py~_cmd_serve  (function, lines 138-150)
# Now read its callers and callees:
get(kind='python', id='precis::precis.cli._cmd_serve')
```

### Understand "how does `precis serve` boot?"

```python
get(kind='python', id='precis', view='entries')
# → precis console-script
#     entry: precis.cli:main
#     file:  precis/src/precis/cli.py:42

get(kind='python', id='precis', view='callgraph',
    entry='precis.cli:main', depth=3)
# → tree of static call edges from main downward

# Drill into the most interesting node from the graph:
get(kind='python', id='precis::precis.runtime.build_runtime')
```

The `entries` view shows entry points in **both forms** — the
`module:function` setuptools notation (used as `entry=` in
`callgraph`) and the file path (use as a normal python id).

Three calls, no `read_file`, no `grep`. The agent has the boot
sequence mapped.

### Find every caller of a function

```python
get(kind='python', id='precis::precis.registry.Registry.get')
# Response includes a "Called by:" section:
#   precis.runtime.build_runtime               1×
#   precis.server.PrecisServer._dispatch       3×
```

The default symbol view *is* the caller/callee view. No separate
view needed for this common question.

### Read just enough source

```python
# Use TOC + outline first.
get(kind='python', id='precis/src/precis/registry.py')

# When you've narrowed it down, read the actual source.
get(kind='python', id='precis::precis.registry.Registry.get', view='source')
```

`source` returns the function body verbatim — same content
`read_file` would give you, but only the lines you actually need.

### Git: who last touched this and why?

```python
get(kind='python', id='precis::precis.registry.Registry.get', view='blame')
get(kind='python', id='precis::precis.registry.Registry.get', view='log')
get(kind='python', id='precis::precis.registry.Registry.get',
    view='churn', days=90)
```

Symbol-scoped (not file-scoped). Renames are followed automatically.

## Editing code

Writes go through the same `put` verb as every file kind, with two
extras specific to python:

- **AST validation** is mandatory — the result must `ast.parse`.
- **`ruff check --fix` then `ruff format`** runs automatically on
  every successful write. Ruff applies safe autofixes (unused
  imports, sorted `__all__`, `is None` over `== None`, etc.) and
  then normalises layout. Both follow the project's `pyproject.toml`
  / `ruff.toml`, so writes match what `ruff check --fix file.py &&
  ruff format file.py` would produce interactively.

If ruff changed the buffer, the response tells you what it did —
not just "applied", but the specific changes (which import was
unused, which `__all__` was re-sorted). Treat that summary as
feedback: the agent learns its style mismatches from one write to
the next, rather than being silently corrected.

### Replace by qualname (preferred)

```python
put(kind='python',
    id='precis::precis.registry.Registry.get',
    text='''    def get(self, kind: str) -> Handler:
        """Look up a handler by kind name."""
        if kind not in self._handlers:
            raise NotFound(
                f"unknown kind: {kind}",
                options=list(self._handlers),
            )
        return self._handlers[kind]''',
    mode='replace')
# Response:
#   replaced precis.registry.Registry.get (lines 120–128 → 120–128)
#   ast.parse:           ok
#   qualname preserved:  ok
#   ruff:                no changes
```

The handler resolves the qualname → file + line range, splices the
replacement, validates with `ast.parse`, runs `ruff check --fix`
then `ruff format`, writes atomically, and re-indexes. The response
gives the **post-fix-and-format** line range — use those numbers in
subsequent calls.

Prefer this form: qualnames survive file moves and re-orderings.

### Replace by line range (when you have line numbers)

```python
put(kind='python',
    id='precis/src/precis/registry.py~L120-128',
    text='        return self._handlers[kind]',
    mode='replace')
# Response:
#   replaced lines 120–128 → 120 in src/precis/registry.py
#   affects symbols: precis.registry.Registry.get
#   ast.parse:       ok
#   ruff:            1 change
#     - 1 whitespace adjustment (format)
```

Line numbers are **1-indexed and inclusive on both ends** (same as
vi, sed, GitHub permalinks, Python tracebacks). `L120-128` is
lines 120, 121, …, 128 — 9 lines. `L120` is line 120 alone.

Use when you have line numbers from grep / a stack trace / a test
failure. Otherwise prefer qualnames.

### Append a new top-level function

```python
put(kind='python',
    id='precis/src/precis/registry.py',
    text='''

def reset_registry() -> None:
    """Clear all registered handlers."""
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = None
''',
    mode='append')
# Response:
#   appended to src/precis/registry.py (lines 156–160 added)
#   new symbols: precis.registry.reset_registry
```

### Create a new file

```python
put(kind='python',
    id='precis/src/precis/handlers/audit.py',
    text='''"""Audit handler."""
from precis.protocol import Handler


class AuditHandler(Handler):
    pass
''',
    mode='create')
```

`mode='create'` refuses to overwrite an existing file; use
`mode='replace'` on a file id (no selector) to swap a whole file.

### Delete a method

```python
put(kind='python',
    id='precis::precis.registry.Registry.deprecated',
    mode='delete')
# Response:
#   deleted precis.registry.Registry.deprecated (lines 145–152)
#   ast.parse:           ok
#   qualname removed:    ok
#   ruff:                2 changes
#     - removed 1 unused import (`json`)  # only used by deleted method
#     - 1 whitespace adjustment (format)
```

### What can go wrong

| Problem | Response |
|---|---|
| Replacement text doesn't parse as Python | `BadInput("ast.parse failed: <error>")` — file untouched |
| Edit drops a qualname that lived inside the addressed region | `BadInput("qualname(s) dropped: Class.method_b, Class.method_c")` — pass `allow_rename=True` to override |
| Path traversal | `BadInput("path outside configured root")` |
| Indentation mismatch | The replacement text is spliced verbatim; you supply the indentation. Use `view='source'` first to get the right level. |
| Line range out of bounds | `BadInput("line range L<a>-<b> outside file (1–<n>)")` |
| Empty line range (end before start) | `BadInput("empty range: end < start")` |

The drop check is one set diff: every qualname that was inside the
addressed region before the edit must still exist in the file
afterwards. Catches both **accidental renames** (rewriting a method
but typing the wrong `def` line) and **accidental drops** (replacing
a whole class but forgetting to copy over some of its methods).
Intentional rename or drop? Pass `allow_rename=True`.

### Workflow: read → modify → write

The canonical edit flow is a three-call round-trip:

```python
# 1. Read the symbol's source.
get(kind='python',
    id='precis::precis.registry.Registry.get',
    view='source')
# → returns the function body with its native indentation

# 2. Modify locally (in your context).

# 3. Write back at the same qualname.
put(kind='python',
    id='precis::precis.registry.Registry.get',
    text=<modified source>,
    mode='replace')
```

Because step 1 returns the indented source and step 3 takes it back
verbatim, indentation is preserved by construction. No re-indenting
logic to get wrong.

### What you handle yourself

- **Imports.** Adding a method that needs a new import? Add the
  import in a separate `put` at the file top. v1 doesn't auto-detect
  imports.
- **Cross-file rename.** A rename is delete + create + reference
  updates across the codebase. v1 does the first two; the editor /
  coding-agent does the reference updates.
- **Commits.** The working tree is left dirty. Commit policy is
  yours.

## What python does NOT do

- **Cross-language.** Python only. `tree-sitter` upgrade path
  exists but is not wired.
- **Type-aware resolution.** Static AST analysis only. `jedi` /
  `pyright` integration is gated behind a future
  `PRECIS_PYTHON_TYPED=1` env var.
- **Type checking on writes.** `mypy` is not run; only `ast.parse`
  + `ruff check --fix` + `ruff format`. Lint findings that ruff
  cannot autofix (e.g. `F821` undefined name) pass through to the
  response as a note but don't block the write — you'll catch them
  on the next test run.
- **Test running.** Out of scope; that's CI territory.
- **Auto-commit.** Writes leave the working tree dirty.
- **Runtime root mutation.** No `register` / `unregister` /
  `reindex` modes on the agent surface; repos are configured at
  startup via `PRECIS_PYTHON_ROOTS`. Reindex happens automatically
  on file mtime change.

## See also

- `precis-files-help` — shared addressing for all file kinds
- `precis-markdown-help` — markdown-specific block grammar
- `precis-overview` — verbs and kinds
