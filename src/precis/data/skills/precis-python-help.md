---
id: precis-python-help
title: precis — navigate and edit Python codebases
applies-to: get/search/put/edit/delete (kind='python')
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

```python
get(kind='python', id='precis')                                       # repo overview
get(kind='python', id='precis/src/precis/cli.py')                     # file outline
get(kind='python', id='precis/src/precis/cli.py~L120')                # one line (Track A)
get(kind='python', id='precis/src/precis/cli.py~L96-130')             # line range
get(kind='python', id='precis/src/precis/dispatch.py~Hub')            # local symbol (Track B)
get(kind='python', id='precis/src/precis/dispatch.py~Hub.register_ability')
get(kind='python', id='precis::precis.dispatch.Hub.register_ability') # qualname shortcut
```

Ambiguous qualnames return `BadInput` with `options=` listing every
matching qualname.

## Find the right place to start
## I'm new to this repo — where do I begin?
## Orient in an unfamiliar Python codebase

```python
search(kind='python', q='cache attribution', scope='precis')
search(kind='python', q='where do we handle stale data', scope='precis', page=2)
```

Hits embed qualname + signature + docstring; results come back as
canonical addresses you can paste as `id=`. `page=1` is the default.

## Read a file or a symbol
## Open Python source

```python
get(kind='python', id='<alias>')                              # repo overview
get(kind='python', id='<alias>', view='toc')                  # module/package tree
get(kind='python', id='<alias>', view='entries')              # console scripts + __main__
get(kind='python', id='<alias>/<path>.py')                    # file outline (default)
get(kind='python', id='<alias>/<path>.py', view='outline')    # outline w/ type annotations
get(kind='python', id='<alias>/<path>.py', view='source')     # raw source
get(kind='python', id='<alias>::<qualname>')                  # signature + docstring + callers + callees
get(kind='python', id='<alias>::<qualname>', view='source')   # body verbatim
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
| `callgraph` | entry-rooted static call tree (needs `args={'entry': ...}`) |
| `runtrace` | dynamic trace; gated by `PRECIS_PYTHON_ALLOW_EXEC=1` |

## Map a stack trace to a symbol
## I have a line number — what symbol is it in?

```python
get(kind='python', id='precis/src/precis/dispatch.py~L444')
# Response resolves L444 → boot (lines 444-612). Then:
get(kind='python', id='precis::precis.dispatch.boot')
```

## Trace a boot path
## How does the `precis` entry point reach this function?
## Walk the call graph from a console script

```python
get(kind='python', id='precis', view='entries')
# → entry: precis.cli:main  (setuptools shorthand)

get(kind='python', id='precis', view='callgraph',
    args={'entry': 'precis.cli.main:main', 'depth': 3})
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
get(kind='python', id='precis::precis.dispatch.Hub.register_ability')
# Default symbol view includes Called by: and Calls: sections.
```

## Edit a symbol by qualname
## Replace a function body — preferred edit form

```python
edit(kind='python',
    id='precis::precis.dispatch.Hub.handler_for',
    text='''    def handler_for(self, kind: str) -> Any | None:
        """Return the handler registered for ``kind``, or None."""
        return self.handlers.get(kind)''',
    mode='replace')
```

```text
replaced precis.dispatch.Hub.handler_for (lines 204-206 → 204-206)
ast.parse:           ok
qualname preserved:  ok
ruff:                no changes
```

Qualnames survive file moves and re-orderings — prefer this form.
The response gives the **post-format** line range; use those in
follow-ups.

## Edit by line range
## Replace lines when I have line numbers

```python
edit(kind='python',
    id='precis/src/precis/dispatch.py~L204-206',
    text='        return self.handlers.get(kind)',
    mode='replace')
```

```text
replaced lines 204-206 → 204
affects symbols: precis.dispatch.Hub.handler_for
ast.parse:       ok
ruff:            1 change
  - 1 whitespace adjustment (format)
```

Line numbers are 1-indexed, inclusive both ends (vi/sed/GitHub
permalink convention). `L120-128` is 9 lines; `L120` is one.

## Surgical edits inside a function
## Rename one call site, fix one literal

```python
# Anchored rename within one symbol.
edit(kind='python',
    id='precis::precis.dispatch.boot',
    mode='find-replace',
    find='deprecated_call(', text='new_call(',
    match='all')

# Disambiguate via surrounding context.
edit(kind='python',
    id='precis/src/precis/dispatch.py',
    mode='find-replace',
    find='name', before='len(', after=')',
    text='full_name')

# Insert adjacent to an anchor.
edit(kind='python',
    id='precis/src/precis/dispatch.py',
    mode='insert',
    find='    return x + 1\n', where='after',
    text='\n\ndef twice(x: int) -> int:\n    return x * 2\n')

# Span delete — text='' is the delete idiom.
edit(kind='python',
    id='precis::precis.dispatch.Hub.handler_for',
    mode='find-replace',
    find='    # TODO: revisit\n', text='')
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
put(kind='python',
    id='precis/src/precis/handlers/audit.py',
    text='''"""Audit handler."""
from precis.protocol import Handler


class AuditHandler(Handler):
    pass
''',
    mode='create')

# Append a top-level function.
edit(kind='python',
    id='precis/src/precis/dispatch.py',
    text='''

def registered_kinds(hub: Hub) -> list[str]:
    """Snapshot of every kind currently registered on ``hub``."""
    return sorted(hub.kinds)
''',
    mode='append')
```

`mode='create'` refuses to overwrite; use `mode='replace'` on a
bare file id to swap a whole file.

## Delete a method
## Drop a symbol from a class

```python
delete(kind='python', id='precis::precis.dispatch.Hub.deprecated')
```

```text
deleted precis.dispatch.Hub.deprecated (lines 145-152)
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

```python
get(kind='skill', id='precis-files-help')        # shared file address grammar
get(kind='skill', id='precis-edit-help')         # anchored find-replace + insert grammar
get(kind='skill', id='precis-markdown-help')     # .md block grammar
get(kind='skill', id='precis-overview')          # verbs and kinds
```
