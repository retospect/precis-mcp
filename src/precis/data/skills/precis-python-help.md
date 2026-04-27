---
id: precis-python-help
title: precis — navigate Python codebases
status: spec (unbuilt)
tier: 1
floor: any
applies-to: get/search (kind='python'); read-only in v1
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

## What python does NOT do

- **Edit code.** Read-only in v1. Editing belongs to the editor /
  coding-agent. The unified file-handler base has the write
  plumbing; opt-in is a deferred decision pending AST-validation
  policy.
- **Cross-language.** Python only. `tree-sitter` upgrade path
  exists but is not wired.
- **Type-aware resolution.** Static AST analysis only. `jedi` /
  `pyright` integration is gated behind a future
  `PRECIS_PYTHON_TYPED=1` env var.
- **Runtime mutation.** No `register` / `unregister` modes; repos
  are configured via `PRECIS_PYTHON_ROOTS` env, not at runtime.

## See also

- `precis-files-help` — shared addressing for all file kinds
- `precis-markdown-help` — markdown-specific block grammar
- `precis-overview` — verbs and kinds
