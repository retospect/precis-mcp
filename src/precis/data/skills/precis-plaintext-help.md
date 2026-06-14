---
id: precis-plaintext-help
title: precis — read and edit plaintext files
summary: plaintext files — txt/log paragraphs and lines, content-slug selectors, raw views
applies-to: get/search/put/edit/delete (kind='plaintext')
status: active
---

# precis-plaintext-help — `.txt` and `.log` files

Unstructured prose: notes, log captures, pasted fragments. No
headings, no fenced code, no tables. For shared address grammar,
two-track addressing, and write modes, see `precis-files-help`.

If you want headings or code fences, use `kind='markdown'` instead.

## What does a plaintext id look like?
## Address a `.txt` or `.log` file
## How do I point at a paragraph in a log?

```python
get(kind='plaintext', id='logs/2026-05.txt')           # path form, with extension
get(kind='plaintext', id='logs--2026-05')              # canonical slug form
get(kind='plaintext', id='logs/2026-05.txt~3')         # paragraph by position
get(kind='plaintext', id='logs/2026-05.txt~opened-laptop-at-0915')  # by content slug
get(kind='plaintext', id='logs/2026-05.txt~L42-58')    # by line range
get(kind='plaintext', id='logs/2026-05.txt/raw')       # full source
```

Both `.txt` and `.log` share one address space. The extension can
appear in the path form; the canonical slug strips it and replaces
`/` with `--` (`logs/2026-05.txt` ↔ `logs--2026-05`). Either resolves.

`~L<n>-<m>` is the line-range selector — useful when something
external (grep, a stack trace, the tail of a log) gave you line
numbers.

## How is a plaintext file divided into blocks?
## What counts as a block in a `.txt` file?
## Paragraph grammar for plaintext

One block = one paragraph: a run of non-blank lines separated from
its neighbours by at least one blank line. That is the whole
grammar. `# foo` is a paragraph that starts with a hash — not a
heading. Fenced code, tables, list markers are all just text.

Paragraph slugs are content-derived (first few words + short hash),
so editing a paragraph changes its slug. Stale references resolve
for one re-ingest cycle via the rename map; the response always
carries the new slug.

## How do I read a plaintext file?
## Open a log and see its contents

```python
get(kind='plaintext')                                  # index of all files
get(kind='plaintext', id='logs/2026-05.txt')           # overview
get(kind='plaintext', id='logs/2026-05.txt~3')         # one paragraph
search(kind='plaintext', q='deployment issue')
search(kind='plaintext', q='deployment issue',
       scope='logs/2026-05.txt')                       # one file
```

## How do I write a plaintext file?
## Create or append to a `.txt`
## Drop a log capture into the corpus

```python
put(kind='plaintext', id='captures/session-2026-05-01',
    text='''Opened laptop at 09:15.

Investigated the PRECIS_ROOT gating issue.

Wrapped up at 11:00.''',
    mode='create')

edit(kind='plaintext', id='captures/session-2026-05-01',
     text='Follow-ups filed.', mode='append')
```

Writes go through verbatim after a UTF-8 encode check — no parse
gate, no formatter. Whatever bytes you send are what lands on disk.

## How do I make a surgical edit in a log?
## Find-replace within one paragraph
## Fix a timestamp without rewriting the block

```python
edit(kind='plaintext', id='captures/session-2026-05-01~opened-laptop-at-0915',
     mode='find-replace',
     find='09:15', text='09:20')

# Delete one line by replacing it with empty text. Anchors disambiguate
# in case the same find= text appears elsewhere in the file.
edit(kind='plaintext', id='refs.bib',
     mode='find-replace',
     find='doi     = {10.1111/ejn.12125}',
     before='@article{tritsch2012dopaminergic,',
     after='volume  = {35},',
     text='')
```

Scope the edit by passing `~<slug>` or `~L<n>-<m>` in `id=` so the
match is bounded to one paragraph or line range. `delete` is for
whole files and whole blocks; for line-level removals use
`find-replace` with `text=''`.

Full edit grammar lives in `precis-edit-help`.

## See also

```python
get(kind='skill', id='precis-files-help')       # shared address grammar, write modes
get(kind='skill', id='precis-edit-help')        # find-replace + insert
get(kind='skill', id='precis-markdown-help')    # use this for structured notes
get(kind='skill', id='precis-tex-help')         # .tex files
get(kind='skill', id='precis-overview')         # verbs and kinds
```
