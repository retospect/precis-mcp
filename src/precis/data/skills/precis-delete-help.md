---
id: precis-delete-help
title: precis — soft-delete a ref or remove a region of a file
applies-to: delete (every kind that supports it)
status: active
---

# precis-delete-help — remove a ref or a selector region

`delete` removes content two ways. Numeric-ref kinds soft-delete:
the row hides from list/search but is recoverable. File kinds
require a selector and rewrite the file without the addressed
region.

## Soft-delete a memory, todo, or other numeric ref
## Remove a note I no longer want
## How do I drop a todo from the list?

```python
delete(kind='memory', id=42)
delete(kind='todo', id=122)
delete(kind='gripe', id=9)
delete(kind='fc', id=204)
delete(kind='citation', id=18)
```

Soft-delete only. The ref disappears from list views and search;
the row is retained for audit. Links pointing at the soft-deleted
ref persist — resolve via the link table if you need them.

## Delete a block or section from a markdown / tex file
## Remove one part of a file without rewriting the whole thing
## How do I drop section X from this file?

```python
delete(kind='markdown', id='notes/foo.md~intro')        # named block
delete(kind='markdown', id='notes/foo.md~3..5')         # block range
delete(kind='tex', id='chapters/intro.tex~background')  # tex section
```

The file is rewritten without the addressed region. The selector
grammar is the same one `get` uses — see `precis-files-help`.

## Delete a line range from a plaintext or log file
## Cut lines 40–60 of this file

```python
delete(kind='plaintext', id='captures/log~L40-L60')
delete(kind='plaintext', id='captures/log~L12')
```

`~L<n>` is a single line; `~L<n>-<m>` is an inclusive range.

## Delete a Python symbol
## Remove a function or class from a module

```python
delete(kind='python', id='r::pkg.module.deprecated_func')
delete(kind='python', id='r::pkg.module.OldClass')
```

The symbol's source span is removed; the rest of the module stays.
`r::` is the repo prefix — see `precis-python-help`.

## Clear an entire file
## I want to empty this file, not delete a region

```python
edit(kind='markdown', id='notes/foo.md', mode='replace', text='')
```

`delete` without a selector on a file kind raises `BadInput` — the
verb refuses to wipe a whole file by accident. Use `edit` with
`mode='replace'` and an empty body.

## Delete a single matched span (one citation, one line)
## Cut one occurrence of a string from a file

```python
edit(kind='plaintext', id='refs.bib',
     mode='find-replace',
     find='doi     = {10.1111/ejn.12125}',
     before='@article{tritsch2012dopaminergic,',
     after='volume  = {35},',
     text='')
```

`delete` operates on whole blocks / line ranges / symbols.
For arbitrary spans inside a block, use `edit(mode='find-replace',
text='')`. See `precis-edit-help`.

## Why can't I delete a paper or a cached tool answer?

```python
delete(kind='paper', id='<slug>')   # raises Unsupported
delete(kind='web', id='<url>')      # raises Unsupported
```

Papers, patents, and cache-backed kinds (`calc`, `math`, `web`,
`youtube`, `websearch`, `think`, `research`) reject `delete`. Cache
entries expire on TTL or via `tag(... add=['CACHE:stale'])`. To
remove an ingested paper, work at the SQL layer or re-fetch by id
to overwrite the local copy.

## Undo a delete
## I deleted the wrong thing — can I get it back?

Soft-deletes (numeric refs) are recoverable at the SQL layer — the
row is still there. Selector deletes on file kinds rewrite the
file; recover from VCS or your editor's undo.

## See also

```python
get(kind='skill', id='precis-edit-help')      # span-delete, find-replace, whole-file clear
get(kind='skill', id='precis-files-help')     # selector grammar for file kinds
get(kind='skill', id='precis-overview')       # verbs and kinds
get(kind='skill', id='precis-memory-help')    # what a soft-deleted memory looks like
get(kind='skill', id='precis-todo-help')      # closing vs deleting a todo
```
