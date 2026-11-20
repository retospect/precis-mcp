---
id: precis-edit-help
family: files
title: precis — anchored region edits across file kinds
summary: anchored region edits — find-replace grammar, before/after anchors, per-kind validation
answers:
  - how do I replace one string in a file without rewriting the whole chunk?
  - how do I rewrite an entire section or function by its handle?
  - what do I do when find= matches more than one place?
  - how do I preview an edit without committing it?
  - how do I edit a Python function by its qualname?
applies-to: edit (kind='markdown'|'plaintext'|'tex'|'python')
status: active
tags: [verbs]
kinds: [markdown, plaintext, tex, python]
scope: write
---

# precis-edit-help — anchored region edits across file kinds

Project prose lives in the memory mesh: find the project hub with `search(kind='memory', tags=['project:<slug>'])`, then read its navigation links. Write linked memories or the bound draft; file kinds are import/export only. `meta.workspace` is the export home. Compile/export a draft only when requested. See [[precis-memory-help]] and [[precis-draft-help]].

`edit` rewrites a region of an existing file. The grammar is identical
across `markdown`, `plaintext`, `tex`, and `python`; only the
post-edit validation differs.

**`mode=` varies by kind — this page's four-mode grammar
(`find-replace`/`insert`/`append`/`replace`) is the file-kind shape
only.** `quest` accepts `replace`, `pause`, and `resume`
(pass `replace` explicitly for content edits — the top-level default
`mode='find-replace'` is rejected there); see `precis-quest-help`.
`todo` takes `replace` (title/body) plus `find-replace` / `insert` on its
details body (literal `find=`, unique unless `match=`, `dry_run=True`
previews; comments are untouched); see `precis-todo-tree-help`. `memory` takes `replace` plus the `find-replace` /
`insert` anchors on its body — and `ops=[{find,text,...}]`, several
anchored ops applied in order in one transaction, a bad op refusing the
whole call as `ops[i]` (`precis-memory-help`). `finding` takes `replace`
/ `find-replace`; `fleet` and `learning` take `replace`. Most other
kinds either ignore `mode=` (metadata-patch edits on `paper`/`structure`)
or don't support `edit` at all. A wrong `mode=` raises `BadInput`
naming that kind's actual accepted set. Draft uses `replace-content` to
rewrite a block's own content while keeping its children, and `insert`
(needs `find=`, `text=`, `where=`; refused without `find=`) to splice text
next to an anchor inside a chunk — to add a new paragraph use
`put(kind='draft', ..., args={'at': {'before': 'dc<id>'}})`.

Address the chunk by the **handle** get/search hands you — `mc<id>` for a
markdown chunk, `lc<id>` plaintext, `xc<id>` tex — and paste it straight into
`id=`. Append a `~<selector>` to scope within it (`~L42`, `~L42-58`, a content
slug, …).

After a successful exact-handle `get` within the preceding hour (same
credential; same live MCP session when unauthenticated), `text=` may be
`{"$ref": "<handle>"}` instead of literal text. The server resolves current
bytes at execution, enforces the same receipt and size rules as `put`, and
prepends the handle/length/SHA-256 ack. `dry_run=True` resolves and reports
without writing. Other fields and nested reference shapes are not supported.

## Guard the content read before an edit

`base_sha=` pins the edit to the content you read —
draft chunks accept a ≥8-hex prefix of the read's `sha:`, python
line ranges the `range:` line's `sha=`. The call refuses if the
content changed; a python range whose lines merely moved still
lands (relocated exact-hash run). Omit it to edit regardless.

## Select modes and fields for non-file edits

| Kind | Accepted edit modes |
| --- | --- |
| markdown | `find-replace`, `insert`, `append`, `replace` |
| plaintext | `find-replace`, `insert`, `append`, `replace` |
| tex | `find-replace`, `insert`, `append`, `replace` |
| python | `find-replace`, `insert`, `append`, `replace` |
| todo | `replace`, `find-replace`, `insert`, `delete`, `gate` |
| quest | `replace`, `pause`, `resume` |
| memory | `replace`, `find-replace`, `insert`, `append`, `delete` |
| finding | `replace`, `find-replace` |
| fleet | `replace` |
| learning | `replace` |

Todo's `replace` accepts `text=` (title) and/or `body=` (details).
Pass quest's `replace` explicitly for content edits; its default
`find-replace` is rejected. Memory also takes `ops=[{find,text,...}]`.
Draft's `replace-content` is handled within the handler rather than
declared in `edit_modes`; its other edits use kind-specific fields.
Paper and structure ignore `mode=` for metadata patches.

## Change one token, cite, or literal in a file
## Replace a string somewhere I know exists
## Swap a word inside a paragraph

```python
edit(
    kind="markdown",
    id="mc512",  # the chunk handle from get/search
    mode="find-replace",
    find="the",
    before="over ",
    after=" fence",
    text="a",
)
```

`find=` is literal text. `before=` / `after=` are optional anchors —
strict whitespace — that pin the match when `find=` alone is
ambiguous. `mode='find-replace'` is the default.

## Delete a matched span without touching the surrounding chunk
## Remove a line, cite, or token from a file
## Drop one field from a structured entry

```python
edit(
    kind="plaintext",
    id="refs.bib",
    mode="find-replace",
    find="doi     = {10.1111/ejn.12125}",
    before="@article{tritsch2012dopaminergic,",
    after="volume  = {35},",
    text="",
)
```

On a file kind, delete the match by passing an empty replacement:
`mode='find-replace', find=…, text=''`. (`mode='delete'` with `find=`
only is the memory/todo form — some clients mangle an empty string
before the call reaches the server, which is why that mode exists
there — and a file kind's edit refuses it.) The `delete` verb removes
a whole chunk, a `~L<a>-<b>` line range, or a python symbol; a whole
file needs `args={'confirm': 'delete-file-<slug>'}` on `delete`. A
line or token inside a
chunk is `edit`, not `delete` — see [[precis-delete-help]].

## Rewrite a whole chunk, function, or paragraph
## Replace an entire region by its handle
## Overwrite one section of a file

```python
edit(kind="markdown", id="<slug>~intro", mode="replace", text="<new region body>")

edit(
    kind="python",
    id="precis::precis.cli.main",
    mode="replace",
    text="def main():\n    ...\n",
)
```

`mode='replace'` with `id='<slug>~<selector>'` rewrites one chunk.
With a bare `id='<slug>'` (no selector), it rewrites the whole file.

## Add text next to an existing anchor
## Insert before or after a known string

```python
edit(
    kind="markdown",
    id="notes--foo~intro",
    mode="insert",
    find="## Background",
    where="before",
    text="## Motivation\n\nWhy this matters.\n\n",
)
```

`mode='insert'` requires `find=`, `text=`, and `where='before'|'after'`.

## Append to the end of a file

```python
edit(
    kind="markdown",
    id="notes--log",
    mode="append",
    text="\n## 2026-06-05\n\nAnother entry.\n",
)
```

## Rename an identifier across a file
## Bulk-change every occurrence

```python
edit(
    kind="python",
    id="r/src/precis/cli.py",
    mode="find-replace",
    find="deprecated_call(",
    text="new_call(",
    match="all",
)
```

`match='unique'` (default) requires exactly one hit. `match='first'`
takes the earliest. `match='all'` rewrites every hit. `match='nth'`
with `nth=N` picks the Nth (1-indexed).

## Edit one specific line or line range
## Target a region by line number

```python
edit(kind="plaintext", id="<slug>~L42", mode="replace", text="replacement line\n")

edit(kind="markdown", id="<slug>~L42-58", mode="replace", text="<new region body>")
```

`~L<n>` selects one line; `~L<n>-<m>` selects an inclusive line range.
Available on every file kind.

python `~L<a>-<b>` ids take `base_sha=` (the `sha=` of the `range:` line a
read printed): the edit follows the content if lines shifted, and is
refused if it changed; `delete` takes `base_sha=` too. Detail: `precis-python-help` "Edit by line range".

## Edit a python function or class by qualname
## Rewrite one symbol without touching neighbours

```python
edit(
    kind="python",
    id="precis::precis.cli.main",
    mode="replace",
    text="def main():\n    ...\n",
)

edit(
    kind="python",
    id="precis::precis.cli.MyClass.method",
    mode="find-replace",
    find="return x",
    text="return x + 1",
)
```

Python edits run `ast.parse` on the post-edit buffer. A `def`/`class`
rename is rejected unless you pass `allow_rename=True`. `ruff
check --fix` + `ruff format` run on the result.

## Preview an edit without writing to disk
## Dry-run before committing a risky change

```python
edit(
    kind="python",
    id="r/src/precis/cli.py",
    mode="find-replace",
    find="deprecated_call(",
    text="new_call(",
    match="all",
    dry_run=True,
)

edit(
    kind="markdown",
    id="notes--foo",
    mode="find-replace",
    find="draft",
    text="final",
    dry_run="full",
)
```

`dry_run=True` (or `'diff'`) returns a unified diff. `dry_run='full'`
returns the post-edit region with `> ` markers. Validation gates run
during dry-run, so you see whether the edit *would* validate.

## Check dependants before a ref edit

For store-backed kinds (`memory`, `finding`, `paper`), an `edit` dry-run
appends a `## Dependants` block — the refs that rely on the target,
walked to depth 2 — and the real ack ends with a `dependants: X shown
of Y` line, marked `INCOMPLETE` when the walk was cut. Page the full
set with `get(kind=..., id=N, view='dependants')`; an incomplete count
is not "no dependants". Edits never refuse on dependants — the block
is review surface, not a gate.

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
| `markdown` | re-parse chunks on re-ingest | — |
| `plaintext` | UTF-8 encode check | — |
| `tex` | re-parse sections on re-ingest | — |
| `python` | `ast.parse` + qualname-stable | `ruff check --fix` + `ruff format` |

## edit vs put vs delete

| You want to | Verb |
|---|---|
| Create a new file | `put(kind='<kind>', id='<slug>', text='...', mode='create')` |
| Rewrite a region of an existing file | `edit` |
| Remove a whole file or chunk | `delete` |
| Remove one line or token | `edit` — file kinds: `mode='find-replace', find=…, text=''`; memory/todo: `mode='delete', find=…` |

`put` on a file kind is creation-only; use `edit` to modify.

`draft` (and `proposal`) chunks carry more `edit` params outside this
shared grammar — `move=` (reorder/reparent a chunk), `cell=` (a table
chunk's cell), `sub=` (regex substitute, dry-run by default), `review=`
(record a human sign-off), `authoring=` (let review personas edit inline),
`authors=` (set the byline), `word_target=` (a heading's length budget),
`style=` (a heading's section style), `not_abbrev=`/`is_abbrev=` (silence /
un-silence an abbreviation hint) — all documented in `precis-draft-help`,
not here.

`meta=` merges kind-supported keys into the ref's meta dict: a memory's
`hook` (see `precis-memory-help`), pres's BibTeX attribution fields
(venue/date/authors/…, see `precis-pres-help`), or a draft registry `term`
leaf's attribute bag (see `precis-draft-help`). For example,
`edit(kind='memory', id=N, mode='replace', args={'meta': {'hook': 'Recall when choosing a retry policy'}})`
(top-level `meta=` still works but is deprecated).
A kind whose edit handler doesn't declare it raises `BadInput` naming
`meta` instead of silently swallowing the kwarg.

`review='human'` (+ optional `verdict=`, default `'approved'`) records that
you — the human — signed off on a chunk at its *current* text, e.g.
`edit(kind='draft', id='dc12', args={'review': 'human'})`. Both are **draft-only**:
a gripe/todo takes no verdict — record a resolution with
`put(kind='gripe', id=N, text='resolved: <sha> — <how verified>',
tags=['STATUS:done'])` (`precis-gripe-help`). `authoring='on'|'off'`
is a draft-level (not chunk-level) toggle — `edit(kind='draft',
id='<slug>', authoring='on')` — that lets the `cites`/`structure` review
personas edit the draft inline instead of only filing findings. Both are
in `precis-draft-help` § "Edit, review & retire a chunk".

## Say why you changed it
## Give a reason for the revision log

`reason='…'` works on every kind. Findings, memories, concepts, taxa,
citations and links keep a revision log: one entry per change, holding the
prior state and who made it. The reason goes on that entry:
`edit(kind='finding', id=N, mode='replace', text='…', reason='Cu(111), per
the 2024 STM data')`. Without one, the entry reads `edit(kind='<kind>')`.

## Mark a node considered, or take it back
## Record a trust act on a memory, finding, concept, taxon, todo or gripe

Every ref and link carries an origin (`human`, `extracted`, `computed`,
`generated`; `unknown` for legacy) and a confidence (`hunch`,
`considered`, `unknown`). Your own writes are `generated · hunch`, and a
content edit sets them back to hunch. `considered` is an explicit act
against the content you checked. `get` ends with a `trust:` line that
gives the stamp and its sha:

`edit(kind='memory', id='me12', mode='trust', args={'trust':
{'confidence': 'considered', 'reason': 'checked against fi176861',
'expected_sha': '<sha from the trust line>'}})`.

`'confidence': 'hunch'` demotes. A link takes `'link': <link_id>`. A
stale sha is refused, so re-read and decide again. On `put`,
`args={'trust': {'confidence': 'considered', 'reason': '…'}}` creates
the node considered, and `{'origin': 'extracted'}` marks a verbatim
quotation. You cannot assert `human` origin.

**Review stamps.** When a reviewer marks a node considered as part of a
graph review pass, the reason is the structured stamp
`kg-review:<model>:<YYYY-MM-DD>:ok|fixed` — the reviewing model, the UTC
review date, and `ok` (checked, nothing to change) or `fixed` (the
reviewer changed something first). A reason starting `kg-review:` that
does not match in full is refused. Reviewer and date land queryable —
the recorded act carries `model` and `payload.kg_review` — so coverage
(`get(kind='taxon', id='/coverage')`) and the gardener's fresh-review
skip (`precis-gardener-help`) read them without parsing prose.

## See also

- [[precis-files-help]] — shared address grammar (`~L`, `~N`, qualnames).
- [[precis-markdown-help]] — markdown recipes.
- [[precis-python-help]] — python AST gates + ruff.
- [[precis-plaintext-help]] — plaintext quirks.
- [[precis-put-help]] — creating new files.
- [[precis-delete-help]] — whole-file and whole-chunk removal.
- [[precis-draft-help]] — draft-only edit ops: move/table/review/authoring/….
