---
id: precis-edit-help
title: precis — anchored region edits across file kinds
applies-to: edit (kind='markdown'|'plaintext'|'tex'|'python')
status: active
---

# precis-edit-help — anchored region edits across file kinds

`edit` rewrites a region of an existing file. The grammar is identical
across `markdown`, `plaintext`, `tex`, and `python`; only the
post-edit validation differs.

## Change one token, cite, or literal in a file
## Replace a string somewhere I know exists
## Swap a word inside a paragraph

```python
edit(kind='markdown', id='notes--foo~intro',
     mode='find-replace',
     find='the', before='over ', after=' fence',
     text='a')
```

`find=` is literal text. `before=` / `after=` are optional anchors —
strict whitespace — that pin the match when `find=` alone is
ambiguous. `mode='find-replace'` is the default.

## Delete a matched span without touching the surrounding block
## Remove a line, cite, or token from a file
## Drop one field from a structured entry

```python
edit(kind='plaintext', id='refs.bib',
     mode='find-replace',
     find='doi     = {10.1111/ejn.12125}',
     before='@article{tritsch2012dopaminergic,',
     after='volume  = {35},',
     text='')
```

Pass `text=''` to delete the match. The `delete` verb is for whole
files and whole blocks — not lines or tokens.

## Rewrite a whole block, function, or paragraph
## Replace an entire region by its handle
## Overwrite one section of a file

```python
edit(kind='markdown', id='<slug>~intro',
     mode='replace',
     text='<new region body>')

edit(kind='python', id='precis::precis.cli.main',
     mode='replace',
     text='def main():\n    ...\n')
```

`mode='replace'` with `id='<slug>~<selector>'` rewrites one block.
With a bare `id='<slug>'` (no selector), it rewrites the whole file.

## Add text next to an existing anchor
## Insert before or after a known string

```python
edit(kind='markdown', id='notes--foo~intro',
     mode='insert',
     find='## Background',
     where='before',
     text='## Motivation\n\nWhy this matters.\n\n')
```

`mode='insert'` requires `find=`, `text=`, and `where='before'|'after'`.

## Append to the end of a file

```python
edit(kind='markdown', id='notes--log',
     mode='append',
     text='\n## 2026-06-05\n\nAnother entry.\n')
```

## Rename an identifier across a file
## Bulk-change every occurrence

```python
edit(kind='python', id='r/src/precis/cli.py',
     mode='find-replace',
     find='deprecated_call(', text='new_call(',
     match='all')
```

`match='unique'` (default) requires exactly one hit. `match='first'`
takes the earliest. `match='all'` rewrites every hit. `match='nth'`
with `nth=N` picks the Nth (1-indexed).

## Edit one specific line or line range
## Target a region by line number

```python
edit(kind='plaintext', id='<slug>~L42',
     mode='replace', text='replacement line\n')

edit(kind='markdown', id='<slug>~L42-58',
     mode='replace', text='<new region body>')
```

`~L<n>` selects one line; `~L<n>-<m>` selects an inclusive line range.
Available on every file kind.

## Edit a python function or class by qualname
## Rewrite one symbol without touching neighbours

```python
edit(kind='python', id='precis::precis.cli.main',
     mode='replace',
     text='def main():\n    ...\n')

edit(kind='python', id='precis::precis.cli.MyClass.method',
     mode='find-replace',
     find='return x', text='return x + 1')
```

Python edits run `ast.parse` on the post-edit buffer. A `def`/`class`
rename is rejected unless you pass `allow_rename=True`. `ruff
check --fix` + `ruff format` run on the result.

## Preview an edit without writing to disk
## Dry-run before committing a risky change

```python
edit(kind='python', id='r/src/precis/cli.py',
     mode='find-replace',
     find='deprecated_call(', text='new_call(', match='all',
     dry_run=True)

edit(kind='markdown', id='notes--foo',
     mode='find-replace',
     find='draft', text='final',
     dry_run='full')
```

`dry_run=True` (or `'diff'`) returns a unified diff. `dry_run='full'`
returns the post-edit region with `> ` markers. Validation gates run
during dry-run, so you see whether the edit *would* validate.

## When find= matches more than once
## Disambiguate an ambiguous match

The error lists every candidate with line numbers:

```text
find='the' has 3 matches in notes--foo~intro (match='unique' requires exactly 1):
  L42  The fox jumps over the fence.
  L43  The morning was clear.
```

Next: add `before=` / `after=` anchors, or pick a policy
(`match='first'`, `match='all'`, or `match='nth'` with `nth=N`).

## When find= isn't found
## The literal text I gave doesn't match

The error includes up to three fuzzy nearest lines:

```text
find='dpoamine' not found in notes--neuroscience~abstract
Nearest matches in the region:
  L42  dopamine is a neurotransmitter   (88% similar)
```

Next: copy the exact text from `get(kind='<kind>', id='<slug>~<sel>')`,
or widen `id=` to a larger region.

## When the post-edit buffer fails validation

Kind-specific gates run on the spliced buffer before any disk write.
Failure rolls back; disk is untouched.

```text
ast.parse failed on the post-edit buffer: SyntaxError: ... (line 142)
Next: check the indentation / syntax of the replacement text
```

Per-kind gates:

| Kind | Gate | Format |
|---|---|---|
| `markdown` | re-parse blocks on re-ingest | — |
| `plaintext` | UTF-8 encode check | — |
| `tex` | re-parse sections on re-ingest | — |
| `python` | `ast.parse` + qualname-stable | `ruff check --fix` + `ruff format` |

## edit vs put vs delete

| You want to | Verb |
|---|---|
| Create a new file | `put(kind='<kind>', id='<slug>', text='...')` |
| Rewrite a region of an existing file | `edit` |
| Remove a whole file or block | `delete` |
| Remove one line or token | `edit` with `text=''` |

`put` on a file kind is creation-only; use `edit` to modify.

## See also

```python
get(kind='skill', id='precis-files-help')       # shared address grammar (~L, ~N, qualnames)
get(kind='skill', id='precis-markdown-help')    # markdown recipes
get(kind='skill', id='precis-python-help')      # python AST gates + ruff
get(kind='skill', id='precis-plaintext-help')   # plaintext quirks
get(kind='skill', id='precis-put-help')         # creating new files
get(kind='skill', id='precis-delete-help')      # whole-file and whole-block removal
```
