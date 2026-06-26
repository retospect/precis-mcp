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
get(kind='plaintext', id='lc481')                      # one paragraph by handle — canonical (prefix infers kind)
get(kind='plaintext', id='logs/2026-05.txt')           # whole file by path (file-backed address)
get(kind='plaintext', id='logs--2026-05')              # whole file by slug (legacy input)
get(kind='plaintext', id='logs/2026-05.txt~3')         # paragraph by pos (legacy input; output shows the lc<id> handle to paste)
get(kind='plaintext', id='logs/2026-05.txt~opened-laptop-at-0915')  # by content slug (legacy input)
get(kind='plaintext', id='logs/2026-05.txt~L42-58')    # by line range
get(kind='plaintext', id='logs/2026-05.txt/raw')       # full source
```

A paragraph's canonical address is its **handle** `lc<chunk_id>` (e.g.
`lc481`) — what search/get output shows; paste it straight back. The whole
file is still addressable by path or slug (a legitimate file-backed address):
both `.txt` and `.log` share one address space, the extension can appear in the
path form, and the slug strips it and replaces `/` with `--`
(`logs/2026-05.txt` ↔ `logs--2026-05`). The `~3` / `~<content-slug>` paragraph
selectors are legacy input that still resolve.

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

The legacy content-derived paragraph slug (first few words + short hash)
*mutates* when you edit the paragraph — which is exactly why the stable
`lc<id>` handle is canonical. A stale content-slug still resolves for one
re-ingest cycle via the rename map, and the response always carries the
current handle to paste next time.

## How do I read a plaintext file?
## Open a log and see its contents

```python
get(kind='plaintext')                                  # index of all files
get(kind='plaintext', id='logs/2026-05.txt')           # overview
get(kind='plaintext', id='logs/2026-05.txt~3')         # one paragraph (output shows handle lc<id>; get(id='lc<id>') works too)
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
