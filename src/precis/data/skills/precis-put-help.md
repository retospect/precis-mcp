---
id: precis-put-help
family: core
title: precis — the put verb (create, annotate, import)
summary: the put verb — mint new refs, attach tags and links on create, mode selection
answers:
  - how do I jot a quick note into memory?
  - how do I file a todo or a gripe?
  - how do I create a new markdown, plaintext, or tex file?
  - how do I import a Perplexity report I already paid for in the web UI?
applies-to: put (every kind that supports it)
tags: verbs
status: active
scope: write
---

# precis-put-help — create a new ref

`put` is the create verb. It mints new refs and attaches tags or
links on the same call. Kind-specific options go inside args={...}; an
unknown key fails the call, returning the kind's accepted args= keys.
Rewriting an existing ref's body lives on
`edit`; flipping tags or links on an existing ref lives on `tag`
and `link`.

Create by omitting `id=`: the response returns the new ref's handle
(`me<N>`). Required fields are per kind — each `precis-<kind>-help`
lists them. `mode=` picks the creation door: file kinds
(markdown/plaintext/tex/python) take `'create'`; the paid-import kinds
(`websearch`, `perplexity-reasoning`, `perplexity-research`) take
`'import'`; numeric-ref kinds omit `mode=` — an explicit one is rejected.

`put(kind='gripe'|'todo', id=N, text=...)` appends a comment to an existing
one (no `args=`); older comments are shown as one-liners, `view='comments'`
shows all. On memory, id= with link= adds the edge (a repeat says it was already linked); id= with text= refuses, use edit.
On gripe the append takes `link=`/`rel=` and `tags=`/`untags=` in the
same transaction — comment-then-close is one call. On todo it takes
`tags=`/`untags=` the same way (link edits stay on the `link` verb):
`put(kind='todo', id=42, text='done — shipped',
tags=['STATUS:done'], untags=['waiting-for:reto'])` comments, closes
and drops the waiting tag atomically. Tag-only changes stay on
`tag(add=[...]/remove=[...])`.

`text=` and `body=` are the same prose slot spelled two ways across
kinds (finding says `body=`, memory/gripe/files say `text=`). On a kind
that declares only one spelling the other is accepted as an alias, and
passing both at once errors. `todo` declares both — `text=` is the
title, `body=` the details — and takes either or both.

## Make a retry safe with idem_key
## A put timed out — is it safe to retry?

A `put` that stalls client-side ("server isn't responding") may have
committed anyway — the client can't tell a dropped write from a slow
one, so a blind retry risks double-filing. Pass a caller-chosen
`idem_key=` to make the retry safe:

```python
put(kind="todo", text="Sweep patent backlog.", idem_key="sortie-2026-11:7")
```

A retry carrying the same `idem_key` returns the first write's ref
("existing todo td<N> for idem_key=… — returning that id instead of
creating a duplicate") instead of minting a second one. The key is
stamped on the ref as `meta.idem_key`, deduped per kind under a
transaction-scoped advisory lock, so concurrent same-key puts also
collapse to one ref. Keep the key short and stable (`<session>:<serial>`,
`<job>:<step>`); it is a name for the write, not a place for payload.

Supported on creates for any kind whose `put` declares or forwards the
kwarg — `job` (where it originated), `todo`, `gripe`, `memory`,
`quest`, `taxon`, and the other shared-create-path kinds — and on the
append forms: `put(kind='todo'|'gripe', id=N, text=...)` dedupes the
comment chunk, `put(kind='quest', id=N, text=...)` the logbook entry,
and `put(kind='todo', mode='plan', ...)` dedupes on the minted plan's
root todo. On any other kind `idem_key` is not a dedupe — the
dispatcher refuses to replay it, and the kwarg may be ignored
entirely, so there it buys you nothing. A `put` without `idem_key`
is never retried by the dispatcher either, so unconfirmed writes
still need a manual check-then-refile (see `precis-gripe-help`,
"When precis stops responding").

## Write content you already read without retransmitting it

```python
get(id="dc2445852")
put(kind="memory", text={"$ref": "dc2445852"})
```

The whole `text=` value must be the exact `{"$ref": "<handle>"}` object.
A successful exact-handle `get` earlier in the **same MCP session** is
required; search hits and another session's reads do not count. The write
uses the source's **current** bytes, not a snapshot of the earlier response,
and leads with `dereferenced text <- <handle> (N chars, sha256:…)` without
echoing the body. A chunk handle expands to its exact chunk; a record handle
joins live body chunks in order with a blank line. Unknown, retired,
bodyless, exact-unreadable, oversized, or unreceived references refuse before
mutation. Only `put`/`edit` `text=` accepts this envelope; nested fields,
string interpolation, cursors, and literal-JSON escaping are out of scope.

## Jot a thought into memory
## Capture a quick note I want to keep
## How do I record something for later?

```python
put(kind="memory", text="Wang20 cites our 2024 result indirectly.")
put(
    kind="memory",
    text="Schedule the next sortie review for 2026-Q3.",
    tags=["topic:sortie", "pinned"],
)
put(
    kind="memory",
    text="Heterojunction comparison aligns with our hypothesis.",
    link="pa5",
    rel="cites",
)
```

Omit `id=` to mint a fresh numeric ref. The response returns the new
id and its handle (`me<N>`); read back with `get(id='me<N>')` (the
prefix infers the kind) or `get(kind='memory', id=42)`. The full write
recipe — search first, dedupe, hub/link, the round-trip check — is
`precis-memory-help`'s "Write a memory that will be found later"
section.

## File a todo
## Add a task to my list
## I need to remember to do X

```python
put(kind="todo", text="Review section 3 of abazari2024design.")
put(
    kind="todo",
    text="Sweep open patents for cpc B01J27.",
    tags=["PRIO:high", "project:noxrr"],
)
```

`STATUS:open` is the implicit default; flip with
`tag(kind='todo', id=42, add=['STATUS:done'])` once the task lands.

## Log a gripe
## Capture an annoyance to fix later
## How do I record a niggle?

```python
put(kind="gripe", text="Search misses on exact CPC codes — needs a lexical floor.")
put(
    kind="gripe",
    text="TOC excerpts feel too short on very long segments.",
    tags=["area:search"],
)
```

Gripes feed the retrieval-misses channel; one sentence per gripe.

## Stash an Anki card
## Create a spaced-repetition cloze card
## How do I queue something to review?

```python
put(
    kind="anki",
    text="The bare-DOI form of get(kind=paper) resolves via {{c1::metadata}}, then fetches the paper.",
)
put(
    kind="anki",
    text="RRF fuses {{c1::lexical}} and {{c2::semantic}} search legs.",
    tags=["topic:precis"],
)
```

Cloze cards (`{{cN::…}}`) sync to AnkiWeb; Anki owns scheduling — see `precis-anki-help`.

## Record a verified citation
## Stamp a claim with a source quote
## How do I write the verifier output back?

```python
put(
    kind="citation",
    text="Z-scheme NOxRR achieves 78% selectivity at 1.2 V.",
    link="pa5",
    rel="cites",
    args={
        "source_handle": "wang2020state~38..42",
        "source_quote": "...selectivity reached 78% at 1.2 V vs RHE...",
        "verifier_confidence": 0.92,
    },
)
```

Citation is the verifier's output kind. See `precis-citation-help`
for the verifier loop and the named-kwarg shape.

## Create a markdown file under PRECIS_ROOT
## Drop a new note file on disk
## How do I make a new .md file?

```python
put(
    kind="markdown",
    mode="create",
    id="notes/proj-fbproj.md",
    text="# fbproj — project notes\n\n## Goals\n- ...\n",
)
```

`mode='create'` is required for file kinds and is the only accepted
mode on `put`. `id=` is the path under `PRECIS_ROOT`. Append, insert,
replace, and find-replace live on `edit`.

## Create a plaintext, tex, or python file
## Drop a new .txt, .tex, or source file
## Same shape, different kind

```python
put(kind="plaintext", mode="create", id="logs/2026-06-05.txt", text="...")
put(kind="tex", mode="create", id="chapters/intro.tex", text="\\section{Intro}\n...")
put(
    kind="python",
    mode="create",
    id="precis::precis.utils.demo",
    text="def demo():\n    ...",
)
```

Same `mode='create'` discipline as `markdown`. The `id=` form for
`python` is `<repo>::<dotted.path>`; see `precis-python-help`.

## Import a Perplexity report I already paid for
## Stash a web-UI Sonar answer at $0
## How do I avoid double-paying for a Perplexity query I already ran?

```python
put(
    kind="websearch",
    mode="import",
    id="latest perovskite tandem efficiencies",
    text="<paste the answer body>",
)
put(kind="perplexity-reasoning", mode="import", id="compare DAC and BECCS", text="...")
put(kind="perplexity-research", mode="import", id="mechanism of NOxRR", text="...")
```

`mode='import'` lands the report in the cache keyed on `id=` (the original
query); the
next `get(kind='websearch', q='...')` hits cache at $0. See
`precis-perplexity-help`.

## Tag a ref while creating it
## Attach metadata on the same call
## How do I avoid a second tag() call?

```python
put(kind="memory", text="...", tags=["pinned", "topic:sortie"])

put(kind="todo", text="...", tags=["PRIO:high"])
```

`tags=` runs at creation — and on a gripe or todo comment append, where it
lands in the same transaction (see above). `PRIO:` (or `prio=`) is an
integer 1..10, lower = hotter, on every kind including gripes;
`urgent`/`high`/`normal`/`low` are aliases for 1/3/5/8. Everywhere else, for
retroactive tag changes use
`tag(kind='...', id=<N>, add=[...], remove=[...])`. Closed-prefix
axes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`) are kind-gated; open
tags are universal. See `precis-tag-help`.

## Link a ref while creating it
## Attach an outbound edge on the same call
## How do I cite a paper from a new memory?

```python
put(kind="memory", text="Anchors our claim.", link="pa5", rel="cites")
put(
    kind="memory",
    text="Touches the same idea.",
    link="pc38",
    rel="related-to",
)
```

`link=` takes a single target `kind:identifier[~selector]` and `rel=`
defaults to `related-to`. For retroactive link changes use the `link`
verb. See `precis-link-help` for the relation vocabulary.

## Annotate a patent I can't put-create
## Attach a note to a read-only ref
## How do I add commentary to a patent?

Patents are read-only via OPS — `put(kind='patent', ...)` is
rejected. Hang a memory off the patent instead:

```python
put(
    kind="memory",
    text="Verification batch — see patent.",
    link="patent:ep4123456a1",
    rel="cites",
)
```

Same trick works for any read-only kind.

## See also

- [[precis-overview]] — seven verbs, address grammar
- [[precis-edit-help]] — sub-region rewrites of existing refs
- [[precis-delete-help]] — soft-delete numeric refs, region delete on files
- [[precis-tag-help]] — tag vocabulary and axis gating
- [[precis-link-help]] — relation vocabulary
- [[precis-citation-help]] — verifier-workflow citation shape
- [[precis-perplexity-help]] — mode='import' for paid kinds
- [[precis-files-help]] — file-kind addressing
