---
id: precis-record-markers-help
family: addressing
title: precis — record markers in responses and quoting records in bodies
summary: the <record>/<body>/<run> tags that bound every read, and the <quote src> convention for quoting one record inside another
answers:
  - what do the <record ...> and </record ...> lines around a get response mean?
  - how do I tell a closed todo or gripe from an open one in a read?
  - what is the b= nonce in a record marker for?
  - what does <body> ... </body> inside a record mean?
  - what is a <run> wrapper around paper chunks?
  - how do I quote or embed another record inside a body I am writing?
  - why is a stored </record> in a body backslash-escaped?
applies-to: get (every refs-backed kind), put/edit text quoting a record
tags: [addressing, workflow]
kinds: [todo, gripe, memory, paper, draft, plan]
status: active
---

# precis-record-markers-help — record boundaries, status and quoting

Every `get` that reads an authored record wraps the response in an
XML-style open/close pair so a reader never has to infer where one
record ends and its status lines live (gr739333). The tags are not
strict XML — no parser and no entity escaping — just a small fixed
vocabulary spelled identically by every kind.

## The read vocabulary

```text
<record td646972 kind=todo status=done closed=2026-11-19 b=A486>
… record text …
</record td646972 status=done b=A486>
```

- `<record <handle> kind=<k> status=<s> [closed=<date>] b=<nonce>>`
  opens one record; `</record <handle> status=<s> b=<nonce>>` closes
  it, restating handle and status for a skimming reader.
- `status=` is the record's live `STATUS:` value (`open`, `done`,
  `won't-do`, `wontfix`, …), `retired` on a tombstone read, or `live`
  for a kind with no lifecycle axis.
- `b=` is a short nonce minted fresh **per response** (a batch get
  shares one across its records). Stored text can spell `<record` but
  can never know the nonce, so a quoted `</record …>` cannot forge the
  real close tag. The server also backslash-escapes marker lookalikes
  in stored text on the way out.
- `<body>` … `</body>` inside a record fences the author's stored text
  from system metadata (headers, `tags:`, link tables, `Next:` lines).
  Everything between the fence lines is what someone wrote; everything
  outside is server-generated.
- `<run <handle> b=<nonce>>` … `</run …>` wraps a window of source
  chunks (paper/draft `~A..B` reads) once — chunk heads inside keep
  their one-line form with no per-chunk close tag.
- Table reads (search results, `view='links'`, `view='dependants'`)
  carry no tags — the row is the boundary. Lifecycle kinds add a
  `status` cell / `[status]` token on the row.

## Closed and redirected records

A terminal record (`done`, `won't-do`, `wontfix`, `resolved`,
`abandoned`) announces itself twice in plain prose inside the frame:
a `CLOSED <date> (<status>) — historical record only, do not act on
the text below` first line and a matching line just above the close
tag, so a reader that skims either end sees it. A retired ref that
merged says where it went: `re-pointed to <handle>`. Treat the text of
a closed record as history, not live work.

## Quoting a record inside a body — `<quote src>`

When a body you write (a todo spec, a gripe comment, a memory)
embeds another record's text, wrap the quotation naming its source
handle so a reader never confuses the quoted record's metadata with
the containing record's:

```text
<quote src=gr716214>
## gr716214: put(kind='memory' …
tags: STATUS:open
</quote gr716214>
```

`<quote src=…>` … `</quote <handle>>` is the one tag stored bodies are
expected to keep — the server passes it through unescaped. Quote the
record's own tag block *inside* the quote, so `tags:` lines read as
belonging to the quoted record, never to the one holding the text.
