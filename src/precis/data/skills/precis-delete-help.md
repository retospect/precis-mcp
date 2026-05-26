---
id: precis-delete-help
title: precis — the delete verb (soft-delete or selector-delete)
status: active
tier: 1
floor: any
applies-to: delete (every kind that supports it)
last-updated: 2026-05-24
---

# precis-delete-help — remove a ref or a selector region

`delete` removes refs (soft) and addressed regions (hard).
Behaviour is kind-specific and explicit by design — the verb does
not infer intent from id shape.

```python
delete(kind='memory', id=42)                   # soft-delete a numeric ref
delete(kind='markdown', id='notes/foo.md~intro')  # delete a block
```

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Which kind to delete from. |
| `id` | str / int | required | Ref id, or `slug~SELECTOR` for region deletes on file kinds. |

## Behaviour by kind family

### Numeric-ref kinds — soft-delete

`memory`, `todo`, `gripe`, `fc`, `quest`, `oracle`, `conv`:

- Soft-deletes the ref. The row is **retained** for audit /
  undelete; it just stops appearing in list views and search.
- Recoverable at the SQL layer.

```python
delete(kind='memory', id=42)
```

### File kinds with a selector — region-delete

`markdown`, `plaintext`, `tex`, `python`:

- With a selector in `id=`: deletes the addressed block / symbol /
  line range. The file is rewritten without the deleted region.
- Without a selector → `BadInput`. The verb refuses to wipe a
  whole file by accident.

```python
# Delete one block of a markdown file.
delete(kind='markdown', id='notes/foo.md~intro')

# Delete one symbol from a python module.
delete(kind='python', id='r::pkg.module.deprecated_func')

# Delete a line range.
delete(kind='plaintext', id='captures/log~L40-L60')
```

### Whole-file delete: use `edit`

To clear an entire file, use `edit(mode='replace', text='')`:

```python
edit(kind='markdown', id='notes/foo.md',
     mode='replace', text='')
```

To delete a matched span (one cite, one line), use
`edit(mode='find-replace', text='')`:

```python
edit(kind='plaintext', id='refs.bib',
     mode='find-replace',
     find='doi     = {10.1111/ejn.12125}',
     before='@article{tritsch2012dopaminergic,',
     after='volume  = {35},',
     text='')
```

The `delete` verb is for **whole files and whole blocks**, not for
lines or tokens. `edit(mode='find-replace', text='')` is the
canonical span-delete idiom — see `precis-edit-help`.

### Cache-backed and read-only kinds — `Unsupported`

`calc`, `math`, `web`, `youtube`, `research`, `think`, `websearch`,
`paper`: `delete` raises `Unsupported`.

For papers and patents — content you didn't author — the safe
operation is to soft-delete via the SQL layer or to re-fetch by
id (which overwrites the local copy). Cache-backed kinds expire
on TTL or via `tag(... add=['CACHE:stale'])`.

## What this verb does NOT do

- **Hard-delete numeric refs.** Soft-delete only. Hard-delete
  lives in DB-admin scripts, not the agent surface.
- **Cascade.** Deleting a memory does not delete linked refs.
  Links are stored separately and persist (pointing at the
  soft-deleted row); resolve via the link table.
- **Undo.** Soft-deletes are recoverable at the SQL layer; selector
  deletes write the file out without the deleted region (recover
  via VCS).
- **Span-delete.** Use `edit(mode='find-replace', text='')`.
- **Whole-file clear.** Use `edit(mode='replace', text='')`.

## See also

- `precis-edit-help` — span-delete via `edit(... text='')`,
  whole-file clear via `edit(mode='replace', text='')`
- `precis-files-help` — selector grammar for file kinds
- `precis-overview` — verbs and kinds
