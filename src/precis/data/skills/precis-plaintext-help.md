---
id: precis-plaintext-help
family: files
title: precis — read and edit plaintext files
summary: plaintext files — txt/log paragraphs and lines, content-slug selectors, raw and links views
answers:
  - how do I point at one paragraph in a .txt or .log file?
  - how do I create or append to a plaintext file?
  - how do I make a surgical find-replace edit inside one paragraph of a log?
applies-to: get/search/put/edit/delete (kind='plaintext')
tags: addressing, drafting
kinds: plaintext
status: active
---

# precis-plaintext-help — `.txt` and `.log` files

Project prose lives in the memory mesh: find the project hub with `search(kind='memory', tags=['project:<slug>'])`, then read its navigation links. Write linked memories or the bound draft; file kinds are import/export only. `meta.workspace` is the export home. Compile/export a draft only when requested. See [[precis-memory-help]] and [[precis-draft-help]].

Unstructured prose: notes, log captures, pasted fragments. No
headings, no fenced code, no tables. For shared address grammar,
two-track addressing, and write modes, see `precis-files-help`.

If you want headings or code fences, use `kind='markdown'` instead.

## What does a plaintext id look like?
## Address a `.txt` or `.log` file
## How do I point at a paragraph in a log?

```python
get(
    kind="plaintext", id="lc481"
)  # one paragraph by handle — canonical (prefix infers kind)
get(kind="plaintext", id="logs/2026-05.txt")  # whole file by path (file-backed address)
get(kind="plaintext", id="logs--2026-05")  # whole file by slug
get(
    kind="plaintext", id="logs/2026-05.txt~3"
)  # paragraph by pos (output shows the lc<id> handle to paste)
get(kind="plaintext", id="logs/2026-05.txt~opened-laptop-at-0915")  # by content slug
get(kind="plaintext", id="logs/2026-05.txt~L42-58")  # by line range
get(kind="plaintext", id="logs/2026-05.txt/raw")  # full source
get(kind="plaintext", id="logs/2026-05.txt", view="links")  # graph links
```

A paragraph's canonical address is its **handle** `lc<chunk_id>` (e.g.
`lc481`) — what search/get output shows; paste it straight back. The whole
file is still addressable by path or slug (a legitimate file-backed address):
both `.txt` and `.log` share one address space, the extension can appear in the
path form, and the slug strips it and replaces `/` with `--`
(`logs/2026-05.txt` ↔ `logs--2026-05`).

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
get(kind="plaintext")  # index of all files
get(kind="plaintext", id="logs/2026-05.txt")  # overview
get(
    kind="plaintext", id="logs/2026-05.txt~3"
)  # one paragraph (output shows handle lc<id>; get(id='lc<id>') works too)
get(
    kind="plaintext", id="logs/2026-05.txt", view="links"
)  # the file's graph links; args={'limit'|'cursor'} pages
search(kind="plaintext", q="deployment issue")
search(kind="plaintext", q="deployment issue", scope="logs/2026-05.txt")  # one file
```

`/raw` returns the file bytes verbatim. `view="links"` lists the
file's graph links, with optional `args.limit` and `args.cursor` for
paging. A path ending in `/links` still addresses a file — pass the
view as an explicit argument when the id is a path.

## How do I write a plaintext file?
## Create or append to a `.txt`
## Drop a log capture into the corpus

```python
put(
    kind="plaintext",
    id="captures/session-2026-05-01",
    text="""Opened laptop at 09:15.

Investigated the PRECIS_ROOT gating issue.

Wrapped up at 11:00.""",
    mode="create",
)

edit(
    kind="plaintext",
    id="captures/session-2026-05-01",
    text="Follow-ups filed.",
    mode="append",
)
```

Writes go through verbatim after a UTF-8 encode check — no parse
gate, no formatter. Whatever bytes you send are what lands on disk.

## How do I make a surgical edit in a log?
## Find-replace within one paragraph
## Fix a timestamp without rewriting the block

```python
edit(
    kind="plaintext",
    id="captures/session-2026-05-01~opened-laptop-at-0915",
    mode="find-replace",
    find="09:15",
    text="09:20",
)
```

Scope the edit by passing `~<slug>` or `~L<n>-<m>` in `id=` so the
match is bounded to one paragraph or line range. `delete` is for
whole files and whole blocks; for line-level removals, including
anchored deletes with `before=`/`after=`, use `find-replace` with
`text=''` — full recipe: [[precis-edit-help]].

## See also

- [[precis-files-help]] — shared address grammar, write modes
- [[precis-edit-help]] — find-replace + insert
- [[precis-markdown-help]] — use this for structured notes
- [[precis-tex-help]] — .tex files
- [[precis-overview]] — verbs and kinds

## Preview a deletion

`delete(kind='plaintext', id='doc~L1-3', dry_run=True)` previews the region and line count without writing. Omit the selector to preview the whole-file delete — a preview needs no confirmation:

```text
delete(kind='plaintext', id='doc', dry_run=True)
→ would delete file 'doc' and retire ref 42 (3 chunks)
```

`reason=` applies only to the whole-file delete — it is recorded on
the retired ref's `deleted` event. A region delete rewrites the file
and retires nothing, so `reason=` there is refused. A file with no
ingested ref retires nothing either — the ack names an unrecorded
`reason=`.
