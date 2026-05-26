---
id: precis-plaintext-help
title: precis — read and edit plaintext files
status: phase-6a
tier: 1
floor: any
applies-to: get/search/put (kind='plaintext')
last-updated: 2026-05-01
---

# precis-plaintext-help — `.txt` / `.log` files

For shared concepts (address grammar, two-track addressing, multi-
root config, write modes, reverse lookups), read `precis-files-help`
first. This skill covers what's specific to `plaintext`.

> **Status:** shipped, gated on `PRECIS_ROOT` (shared with `markdown`
> and `tex`). Use for the long tail: `.txt` notes, `.log` captures,
> pasted fragments, lab notebooks — files that have no block grammar
> and don't need one. If you're writing markdown-flavoured notes, use
> `kind='markdown'` instead — the block structure is genuinely
> useful for headings, code blocks, tables.

## Block grammar

One block = one paragraph (a run of non-blank lines separated from
its neighbours by at least one blank line). No headings, no code
fences, no tables. That's the whole grammar.

| Block kind | Recognized form | Slug shape |
|---|---|---|
| `paragraph` | run of non-blank lines | first ~5 words + 6-hex hash |

Paragraph slugs are content-derived, so **editing one paragraph
changes its slug** but leaves all other slugs stable. The stored
reverse-lookup maps old → new for one re-ingest cycle, so stale
references still resolve with a rename hint (same behaviour as
markdown).

## Address shapes

```python
get(kind='plaintext', id='notes/log-2026')                # overview
get(kind='plaintext', id='notes/log-2026~opened-file')    # one paragraph by slug
get(kind='plaintext', id='notes/log-2026~3')              # one paragraph by pos
get(kind='plaintext', id='notes/log-2026/raw')            # full source
get(kind='plaintext')                                     # index of all files
```

Both `.txt` and `.log` files share the same slug form (the
extension is stripped from the relative path and stored in the
ref's metadata). The handler auto-detects the file's extension on
first touch; once ingested, the extension is pinned in `ref.meta`.

## Recipes

### Dump a log snippet into the corpus

```python
put(kind='plaintext', id='captures/session-2026-05-01',
    text='''Opened laptop at 09:15.

Investigated the markdown kind registration issue —
the skill docs advertise markdown but PRECIS_ROOT
wasn't set in this deployment.

Fixed the skill docs; plaintext kind is shipping in
the same PR.''',
    mode='create')
```

### Append a new paragraph to an existing note

```python
edit(kind='plaintext', id='captures/session-2026-05-01',
     text='Wrapped up the session at 11:00. Follow-ups filed.',
     mode='append')
```

### Surgical edit

Same as every R/W file kind — `edit(mode='find-replace')` with a
literal `find=` plus optional `before=` / `after=` anchors. The
schema lives in `precis-edit-help`.

```python
edit(kind='plaintext', id='captures/session-2026-05-01',
     mode='find-replace',
     find='09:15', text='09:20')
```

The `id` selector bounds the search region: pass `~<slug>` to
scope the edit to one paragraph so you don't accidentally match
the same word elsewhere in the file.

### Delete one line (or one matched span) in place

Use `mode='find-replace'` with `text=''`. That's the canonical
span-delete idiom for every R/W file kind — `delete` is for
whole files and whole blocks, not lines.

```python
# Drop one doi= line from a bibtex entry, leaving the rest of
# the @article{…} block intact. Anchors disambiguate even if the
# same doi text appears elsewhere.
edit(kind='plaintext', id='refs.bib',
     mode='find-replace',
     find='doi     = {10.1111/ejn.12125}',
     before='@article{tritsch2012dopaminergic,',
     after='volume  = {35},',
     text='')   # empty text = delete
```

### Search across all plaintext

```python
search(kind='plaintext', q='deployment issue')
search(kind='plaintext', q='deployment issue',
       scope='captures/session-2026-05-01')   # one file
```

Blocks are embedded (same embedder as the rest of the server), so
semantic search works — `q='markdown gating'` will surface the
paragraph about `PRECIS_ROOT` without needing the exact phrase.

## Limits

- ATX markdown is **not** parsed — `# foo` is just a paragraph
  starting with a hash. Use `kind='markdown'` for real notes.
- No front-matter, no footnotes, no inline link resolution.
- Files larger than ~10 MB are rejected with a hint to chunk
  externally first (same ceiling as markdown).
- `.log` files are read whole — rotate / tail before ingesting
  large ones.

## See also

- `precis-files-help` — shared addressing model for all file kinds
- `precis-edit-help` — universal anchored-edit grammar
- `precis-markdown-help` — markdown block grammar for structured notes
- `precis-tex-help` — `.tex` files (subclasses this kind today)
- `precis-python-help` — code navigation (different parser, same shape)
